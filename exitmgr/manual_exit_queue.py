"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Dict, Iterable


class ManualExitQueueError(RuntimeError):
    pass


def _binding_payload(request: dict) -> dict:
    return {key: request.get(key) for key in (
        "parent_con_id", "symbol", "quantity", "campaign", "journal_identity", "topology",
        "code_version", "policy_version", "action", "order_type", "sec_type")}


def binding_sha(request: dict) -> str:
    raw = json.dumps(_binding_payload(request), sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


class ManualExitQueue:
    SCHEMA = "manual_exit_queue.v2"

    def __init__(self, path):
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.archive_path = Path(str(self.path) + ".archive.jsonl")

    @classmethod
    def _request(cls, raw) -> dict:
        if not isinstance(raw, dict):
            raise ManualExitQueueError("manual-exit request must be a mapping")
        rec = dict(raw)
        try:
            parent = int(rec["parent_con_id"])
            qty = int(rec["quantity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ManualExitQueueError("manual-exit request has invalid parent/quantity") from exc
        if parent <= 0 or qty <= 0:
            raise ManualExitQueueError("manual-exit parent/quantity must be positive")
        request_id = str(rec.get("request_id") or "").strip()
        symbol = str(rec.get("symbol") or "").strip().upper()
        campaign = rec.get("campaign")
        identity = rec.get("journal_identity")
        topology = rec.get("topology")
        code_version = str(rec.get("code_version") or "")
        policy_version = str(rec.get("policy_version") or "")
        action = str(rec.get("action") or "").upper()
        order_type = str(rec.get("order_type") or "").upper()
        sec_type = str(rec.get("sec_type") or "").upper()
        if (not request_id or not symbol or not isinstance(campaign, dict)
                or not isinstance(identity, dict) or not isinstance(topology, list)
                or not topology or not re.fullmatch(r"[0-9a-f]{40}", code_version)
                or not re.fullmatch(r"[0-9a-f]{64}", policy_version)
                or action not in {"BUY", "SELL"} or order_type != "MARKET"
                or sec_type not in {"OPT", "BAG"}):
            raise ManualExitQueueError("manual-exit request is missing immutable binding fields")
        seen_legs = set()
        parent_legs = 0
        for leg in topology:
            if not isinstance(leg, dict):
                raise ManualExitQueueError("manual-exit topology leg must be a mapping")
            try:
                leg_id = int(leg["con_id"])
                position = int(leg["position"])
                if leg_id <= 0 or position == 0 or leg_id in seen_legs:
                    raise ValueError
                seen_legs.add(leg_id)
                if leg_id == parent and str(leg.get("role") or "") == "long":
                    parent_legs += 1
                    if position != qty:
                        raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise ManualExitQueueError("manual-exit topology leg is invalid") from exc
        if parent_legs != 1:
            raise ManualExitQueueError(
                "manual-exit topology must contain one quantity-matched parent long leg")
        rec.update({"parent_con_id": parent, "quantity": qty, "symbol": symbol,
                    "code_version": code_version, "policy_version": policy_version,
                    "action": action, "order_type": order_type, "sec_type": sec_type})
        expected = binding_sha(rec)
        if str(rec.get("binding_sha") or "") != expected:
            raise ManualExitQueueError("manual-exit binding checksum mismatch")
        return rec

    @classmethod
    def _decode(cls, raw) -> Dict[int, dict]:


        if raw == []:
            return {}
        if not isinstance(raw, dict) or raw.get("schema") != cls.SCHEMA:
            raise ManualExitQueueError("manual-exit queue is not schema manual_exit_queue.v2")
        rows = raw.get("requests")
        if not isinstance(rows, list):
            raise ManualExitQueueError("manual-exit requests must be a list")
        out = {}
        for value in rows:
            rec = cls._request(value)
            parent = rec["parent_con_id"]
            if parent in out and out[parent] != rec:
                raise ManualExitQueueError(f"conflicting requests for parent conId {parent}")
            out[parent] = rec
        leg_owner = {}
        for parent, rec in out.items():
            for leg in rec["topology"]:
                leg_id = int(leg["con_id"])
                prior = leg_owner.get(leg_id)
                if prior is not None and prior != parent:
                    raise ManualExitQueueError(
                        f"manual-exit requests {prior} and {parent} share leg conId {leg_id}")
                leg_owner[leg_id] = parent
        return out

    def _mkdir(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise ManualExitQueueError(f"cannot create queue directory: {exc}") from exc

    def _read_unlocked(self) -> Dict[int, dict]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path) as handle:
                return self._decode(json.load(handle))
        except ManualExitQueueError:
            raise
        except Exception as exc:
            raise ManualExitQueueError(
                f"cannot read manual-exit queue {self.path}: {type(exc).__name__}: {exc}") from exc

    def read(self) -> Dict[int, dict]:
        self._mkdir()
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ManualExitQueueError(f"cannot lock manual-exit queue: {exc}") from exc
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_SH)
            return self._read_unlocked()
        finally:
            os.close(fd)

    def _write_unlocked(self, values: Dict[int, dict]) -> None:
        rows = [self._request(values[key]) for key in sorted(values)]



        validated = self._decode({"schema": self.SCHEMA, "requests": rows})
        rows = [validated[key] for key in sorted(validated)]
        tmp = Path(str(self.path) + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"schema": self.SCHEMA, "requests": rows}, handle,
                      sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        try:
            dfd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def add(self, values: Iterable[dict]) -> Dict[int, dict]:
        self._mkdir()
        incoming = [self._request(value) for value in values]
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ManualExitQueueError(f"cannot lock manual-exit queue: {exc}") from exc
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            merged = self._read_unlocked()
            for rec in incoming:
                parent = rec["parent_con_id"]
                prior = merged.get(parent)
                if prior is not None and prior["request_id"] != rec["request_id"]:
                    raise ManualExitQueueError(
                        f"parent conId {parent} already has a different pending request")
                merged[parent] = rec
            self._write_unlocked(merged)
            return merged
        finally:
            os.close(fd)

    def discard(self, parent_con_id: int, request_id: str) -> Dict[int, dict]:
        self._mkdir()
        try:
            parent = int(parent_con_id)
        except (TypeError, ValueError) as exc:
            raise ManualExitQueueError("invalid parent conId for queue clear") from exc
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise ManualExitQueueError(f"cannot lock manual-exit queue: {exc}") from exc
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = self._read_unlocked()
            rec = current.get(parent)
            if rec is None:
                return current
            if rec.get("request_id") != str(request_id or ""):
                raise ManualExitQueueError(
                    f"request identity changed for parent conId {parent}; refusing stale clear")
            del current[parent]
            self._write_unlocked(current)
            return current
        finally:
            os.close(fd)

    def archive_terminal(self, parent_con_id: int, request_id: str,
                         expected_binding_sha: str, evidence: dict) -> Dict[int, dict]:
        """Public API contract; production-derived narrative omitted."""
        if not isinstance(evidence, dict) or evidence.get("broker_flat") is not True:
            raise ManualExitQueueError("terminal archive requires structured broker-flat evidence")
        parent = int(parent_con_id)
        self._mkdir()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = self._read_unlocked()
            rec = current.get(parent)
            if rec is None:
                return current
            if (rec.get("request_id") != str(request_id or "")
                    or rec.get("binding_sha") != str(expected_binding_sha or "")):
                raise ManualExitQueueError(
                    f"request binding changed for parent conId {parent}; refusing stale archive")
            payload = {"schema": "manual_exit_terminal.v1", "request": rec,
                       "binding_sha": rec["binding_sha"], "evidence": evidence}
            archive_key = hashlib.sha256(json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            payload["archive_key"] = archive_key
            payload["archived_at"] = evidence.get("observed_at")
            seen = False
            try:
                with open(self.archive_path) as handle:
                    for line in handle:
                        try:
                            if json.loads(line).get("archive_key") == archive_key:
                                seen = True
                                break
                        except (json.JSONDecodeError, AttributeError):
                            continue
            except FileNotFoundError:
                pass
            if not seen:
                afd = os.open(self.archive_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.fchmod(afd, 0o600)
                with os.fdopen(afd, "a") as handle:
                    handle.write(json.dumps(payload, sort_keys=True, allow_nan=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            del current[parent]
            self._write_unlocked(current)
            return current
        finally:
            os.close(fd)
