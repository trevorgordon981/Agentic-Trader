#!/usr/bin/env python3
"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import build_collaboration_snapshot as snapshot
import sync_collaboration_current as sync


SCHEMA = "alfred-collaboration-current-maintenance-v1"
STATE = Path.home() / ".hermes" / "rag-staging" / "collaboration-current.maintenance.json"
LOCK = Path.home() / ".hermes" / "rag-staging" / "collaboration-current.maintenance.lock"
RAG_HOST_HOST = "rag-host"
RAG_HOST_PATH = "/home/rag-service/alfred-rag-active/data/memory/collaboration-current.md"
RAG_ENDPOINT = "http://127.0.0.1:9000"
RAG_HOST_MANIFEST = "/home/rag-service/alfred-rag-active/indexes/manifest.json"
VERIFY_TIMEOUT_SECONDS = 240
VERIFY_POLL_SECONDS = 5
COMMAND_TIMEOUT_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 15
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CHUNK_ID = re.compile(r"^[0-9a-f]{16}$")

_REMOTE_MANIFEST_READER = r"""
import hashlib, json, os, stat, sys
manifest_path, document_path = sys.argv[1:]
with open(manifest_path, "r", encoding="utf-8") as handle:
    manifest = json.load(handle)
entry = manifest.get(document_path)
if not isinstance(entry, dict):
    raise SystemExit("document absent from manifest")
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
fd = os.open(document_path, flags)
try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("document is not regular")
    digest = hashlib.sha256()
    while True:
        block = os.read(fd, 1 << 20)
        if not block:
            break
        digest.update(block)
    final = os.fstat(fd)
    if (final.st_size, final.st_mtime_ns, final.st_ino) != (
        info.st_size, info.st_mtime_ns, info.st_ino
    ):
        raise SystemExit("document changed while hashing")
finally:
    os.close(fd)
print(json.dumps({
    "source_path": document_path,
    "file_sha256": digest.hexdigest(),
    "file_bytes": info.st_size,
    "file_mtime": info.st_mtime,
    "manifest_mtime": entry.get("mtime"),
    "chunk_count": entry.get("chunk_count"),
    "chunk_ids": entry.get("chunk_ids"),
}, sort_keys=True))
""".strip()


class MaintenanceError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_state() -> dict:
    try:
        raw = snapshot._read_regular_nofollow(STATE)
        parsed = json.loads(raw.decode("utf-8", "strict"))
    except (snapshot.SnapshotError, OSError, UnicodeError, ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_state(value: dict) -> None:
    snapshot._atomic_write(
        STATE,
        (json.dumps(value, sort_keys=True) + "\n").encode("utf-8"),
    )


def _write_failure(exc: BaseException, *, now: datetime) -> None:
    previous = _load_state()
    failure = {
        "schema": SCHEMA,
        "status": "STALE_UNVERIFIED",
        "checked_at": _stamp(now),
        "error_type": type(exc).__name__,
        "error": str(exc)[:800],
    }
    for key in (
        "last_verified_at",
        "source_sha256",
        "output_sha256",
        "published_output_sha256",
        "indexed_source_sha256",
    ):
        if key in previous:
            failure[key] = previous[key]
    _write_state(failure)


def _write_refreshing(*, source_sha: str, output_sha: str, now: datetime, reason: str) -> None:
    previous = _load_state()
    refreshing = {
        "schema": SCHEMA,
        "status": "REFRESHING_UNVERIFIED",
        "checked_at": _stamp(now),
        "source_sha256": source_sha,
        "output_sha256": output_sha,
        "reason": reason,
    }
    if "last_verified_at" in previous:
        refreshing["last_verified_at"] = previous["last_verified_at"]
    _write_state(refreshing)


def _run(command: list[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MaintenanceError(f"command failed: {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise MaintenanceError(f"command exited {result.returncode}: {command[0]}: {detail}")
    return result


def remote_sha256(path: str = RAG_HOST_PATH) -> str | None:
    try:
        result = _run([
            "/usr/bin/ssh", "-o", "BatchMode=yes", RAG_HOST_HOST,
            "/usr/bin/sha256sum", path,
        ])
    except MaintenanceError as exc:
        if "No such file or directory" in str(exc):
            return None
        raise
    token = result.stdout.strip().split(maxsplit=1)[0] if result.stdout.strip() else ""
    if not _SHA256.fullmatch(token):
        raise MaintenanceError("RAG host returned an invalid destination SHA-256")
    return token


def publish_to_rag_host(expected_sha: str) -> None:
    temporary = f"{RAG_HOST_PATH}.candidate.{os.getpid()}"
    _run([
        "/usr/bin/rsync", "-az",
        str(sync.OUTPUT), f"{RAG_HOST_HOST}:{temporary}",
    ])
    _run([
        "/usr/bin/ssh", "-o", "BatchMode=yes", RAG_HOST_HOST,
        "/bin/chmod", "600", temporary,
    ])
    if remote_sha256(temporary) != expected_sha:
        raise MaintenanceError("RAG host candidate hash differs from local snapshot")
    _run([
        "/usr/bin/ssh", "-o", "BatchMode=yes", RAG_HOST_HOST,
        "/bin/mv", "--", temporary, RAG_HOST_PATH,
    ])
    if remote_sha256() != expected_sha:
        raise MaintenanceError("RAG host promoted snapshot hash differs from local snapshot")


def _request_json(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{RAG_ENDPOINT}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            parsed = json.loads(response.read().decode("utf-8", "strict"))
    except (urllib.error.URLError, OSError, TimeoutError, UnicodeError, ValueError) as exc:
        raise MaintenanceError(f"RAG host {path} failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MaintenanceError(f"RAG host {path} returned a non-object response")
    return parsed


def _get_json(path: str) -> dict:
    request = urllib.request.Request(f"{RAG_ENDPOINT}{path}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            parsed = json.loads(response.read().decode("utf-8", "strict"))
    except (urllib.error.URLError, OSError, TimeoutError, UnicodeError, ValueError) as exc:
        raise MaintenanceError(f"RAG host {path} failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MaintenanceError(f"RAG host {path} returned a non-object response")
    return parsed


def health_evidence() -> dict:
    data = _get_json("/health")
    indexes = data.get("indexes") or {}
    lance = indexes.get("lancedb")
    tantivy = indexes.get("tantivy")
    if data.get("status") != "ok" or not isinstance(lance, int) or lance <= 0 or lance != tantivy:
        raise MaintenanceError(f"RAG host index health/parity failed: {data}")
    return {"status": "ok", "lancedb": lance, "tantivy": tantivy}


def manifest_evidence(expected_output_sha: str) -> dict:
    remote = shlex.join([
        "/usr/bin/python3", "-c", _REMOTE_MANIFEST_READER,
        RAG_HOST_MANIFEST, RAG_HOST_PATH,
    ])
    result = _run([
        "/usr/bin/ssh", "-o", "BatchMode=yes", RAG_HOST_HOST, remote,
    ])
    try:
        evidence = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise MaintenanceError(f"invalid RAG host manifest evidence: {exc}") from exc
    chunk_ids = evidence.get("chunk_ids")
    count = evidence.get("chunk_count")
    if evidence.get("source_path") != RAG_HOST_PATH:
        raise MaintenanceError("manifest source path mismatch")
    if evidence.get("file_sha256") != expected_output_sha:
        raise MaintenanceError("manifest document SHA differs from expected output")
    if evidence.get("file_mtime") != evidence.get("manifest_mtime"):
        raise MaintenanceError("manifest mtime differs from active document")
    if (
        not isinstance(chunk_ids, list)
        or not isinstance(count, int)
        or count <= 0
        or count != len(chunk_ids)
        or len(set(chunk_ids)) != count
        or any(not isinstance(item, str) or not _CHUNK_ID.fullmatch(item) for item in chunk_ids)
    ):
        raise MaintenanceError("manifest chunk identity/count is invalid")
    return evidence


def index_has_source(source_sha: str) -> bool:

    health_evidence()
    data = _request_json("/search", {
        "query": f"current collaboration state source_sha256 {source_sha}",
        "top_k": 20,
        "rerank": False,
        "max_per_source": 2,
    })
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        filename = str(result.get("filename") or result.get("source") or "")
        text = str(result.get("text") or "")
        if filename.endswith("collaboration-current.md") and f"source_sha256: {source_sha}" in text:
            return True
    return False


def trigger_ingest() -> dict:
    response = _request_json("/ingest", {"incremental": True})
    if response.get("status") not in {"started", "already_running"}:
        raise MaintenanceError(f"unexpected RAG host ingest response: {response}")
    return response


def _await_index(
    source_sha: str,
    *,
    searcher: Callable[[str], bool] = index_has_source,
    ingester: Callable[[], dict] = trigger_ingest,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    deadline = monotonic() + VERIFY_TIMEOUT_SECONDS
    response = ingester()
    while monotonic() < deadline:
        if searcher(source_sha):
            return response
        sleeper(VERIFY_POLL_SECONDS)


        response = ingester()
    raise MaintenanceError("timed out waiting for exact source SHA in active RAG index")


def maintain_once(
    *,
    now: datetime | None = None,
    syncer: Callable[[], tuple[dict, bool]] = sync.sync_current,
    remote_hasher: Callable[[], str | None] = remote_sha256,
    publisher: Callable[[str], None] = publish_to_rag_host,
    searcher: Callable[[str], bool] = index_has_source,
    awaiter: Callable[[str], dict] = _await_index,
    manifest_reader: Callable[[str], dict] = manifest_evidence,
    health_reader: Callable[[], dict] = health_evidence,
) -> dict:
    now = now or _utc_now()
    previous = _load_state()
    receipt, changed = syncer()
    source_sha = receipt.get("source_sha256")
    output_sha = receipt.get("output_sha256")
    if not isinstance(source_sha, str) or not _SHA256.fullmatch(source_sha):
        raise MaintenanceError("sync receipt has invalid source SHA-256")
    if not isinstance(output_sha, str) or not _SHA256.fullmatch(output_sha):
        raise MaintenanceError("sync receipt has invalid output SHA-256")
    try:
        local_sha = snapshot.sha256(snapshot._read_regular_nofollow(sync.OUTPUT))
    except (snapshot.SnapshotError, OSError) as exc:
        raise MaintenanceError(f"cannot verify local snapshot: {exc}") from exc
    if local_sha != output_sha:
        raise MaintenanceError("local snapshot differs from sync receipt")

    refreshing = previous.get("source_sha256") != source_sha
    if refreshing:
        _write_refreshing(
            source_sha=source_sha,
            output_sha=output_sha,
            now=now,
            reason="authoritative_source_changed",
        )

    remote_sha = remote_hasher()
    published = remote_sha != output_sha
    if published:
        if not refreshing:
            _write_refreshing(
                source_sha=source_sha,
                output_sha=output_sha,
                now=now,
                reason="published_document_mismatch",
            )
            refreshing = True
        publisher(output_sha)
        remote_sha = remote_hasher()
    if remote_sha != output_sha:
        raise MaintenanceError("RAG host destination differs after publication")

    ingest = None
    indexed = searcher(source_sha)
    if not indexed:
        if not refreshing:
            _write_refreshing(
                source_sha=source_sha,
                output_sha=output_sha,
                now=now,
                reason="active_index_mismatch",
            )
            refreshing = True
        ingest = awaiter(source_sha)
        indexed = searcher(source_sha)
    if not indexed:
        raise MaintenanceError("active RAG index does not return the exact source SHA")

    manifest = manifest_reader(output_sha)
    health = health_reader()

    verified_at = _stamp(now)
    state = {
        "schema": SCHEMA,
        "status": "VERIFIED_CURRENT",
        "checked_at": verified_at,
        "last_verified_at": verified_at,
        "source": receipt.get("source"),
        "source_sha256": source_sha,
        "output_sha256": output_sha,
        "published_output_sha256": remote_sha,
        "indexed_source_sha256": source_sha,
        "index": {
            "source_path": manifest["source_path"],
            "file_mtime": manifest["file_mtime"],
            "chunk_count": manifest["chunk_count"],
            "chunk_ids": manifest["chunk_ids"],
            "verified_at": verified_at,
        },
        "health": health,
        "sync_changed": bool(changed),
        "published": published,
        "ingest_status": ingest.get("status") if isinstance(ingest, dict) else "not_needed",
    }
    _write_state(state)
    return state


def main() -> int:
    LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"schema": SCHEMA, "status": "ALREADY_RUNNING"}, sort_keys=True))
            return 0
        now = _utc_now()
        try:
            state = maintain_once(now=now)
        except Exception as exc:
            _write_failure(exc, now=now)
            print(f"collaboration-current maintenance failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(state, sort_keys=True))
        return 0
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
