"""Public API contract; production-derived narrative omitted."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid


class RecoveryError(Exception):
    pass


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite JSON constant")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite JSON number")
    return result


def analyze(body, result, schema, parse_stage_a):
    """Public API contract; production-derived narrative omitted."""
    out = {"eligible": False, "reason": "not_exact_stage_a", "lengths": []}
    contract = body.get("response_format", {}).get("json_schema", {})
    if (contract.get("name") != "stage_a_intents" or contract.get("strict") is not True
            or contract.get("schema") != schema):
        return out
    try:
        choices = result.get("choices", [])
        if len(choices) != 1:
            raise ValueError("ambiguous choices")
        choice = choices[0]
        message = choice.get("message", {})
        if (choice.get("finish_reason") != "stop" or message.get("tool_calls")
                or message.get("refusal")):
            raise ValueError("not_complete_plain_json")
        content = message["content"]
        if not isinstance(content, str) or len(content.encode()) > 1024 * 1024:
            raise ValueError("editorial input exceeds 1MiB bound or is not text")
        parsed = json.loads(content, object_pairs_hook=_unique,
                            parse_constant=_constant, parse_float=_float)
        if not isinstance(parsed, dict) or set(parsed) != {"intents"}:
            raise ValueError("invalid_top_level")
        if not isinstance(parsed["intents"], list) or not parsed["intents"]:
            raise ValueError("empty_or_invalid_intents")
        canonical = {"intents": []}
        editable = []
        proof = {"intents": []}
        for index, row in enumerate(parsed["intents"]):
            if not isinstance(row, dict):
                raise ValueError("invalid_intent")
            clean = {key: value for key, value in row.items()
                     if key not in ("thesis_note", "target_dte_note")}
            for field, maximum in (("alpha", 600), ("thesis", 1200)):
                value = clean.get(field)
                out["lengths"].append({"intent_index": index, "field": field,
                                       "stage": "baseline",
                                       "observed": len(value) if isinstance(value, str) else None,
                                       "minimum": 1, "maximum": maximum})
                if not isinstance(value, str) or not value:
                    raise ValueError("missing_empty_or_nonstring_text")
            for key in ("thesis_note", "target_dte_note"):
                if key in row:
                    if not isinstance(row[key], str) or not row[key].strip():
                        raise ValueError("invalid_explanatory_note")
                    clean["thesis"] += "\n\n" + row[key]
            candidate = copy.deepcopy(clean)
            for field, maximum in (("alpha", 600), ("thesis", 1200)):
                value = clean[field]
                out["lengths"].append({"intent_index": index, "field": field,
                                       "stage": "folded", "observed": len(value),
                                       "minimum": 1, "maximum": maximum})
                if len(value) > maximum:
                    editable.append([index, field])


                    candidate[field] = "length-check-placeholder"
            canonical["intents"].append(clean)
            proof["intents"].append(candidate)
        parse_stage_a(json.dumps(proof, allow_nan=False))
        import jsonschema
        jsonschema.Draft202012Validator(schema).validate(proof)
        if not editable:
            raise ValueError("not_text_overlength")
        out.update(eligible=True, reason="text_overlength_only", canonical=canonical,
                   editable=editable)
    except Exception as exc:
        out["reason"] = str(exc)[:300] if isinstance(exc, ValueError) else type(exc).__name__
    return out


def receipt(body, result, analysis, runtime_before, attempt, outcome, directory=None):
    """Public API contract; production-derived narrative omitted."""
    configured = os.environ.get("EXITMGR_SCHEMA_REJECTION_DIR")
    root = (Path(directory) if directory is not None else Path(configured) if configured else
            Path.home() / ".local" / "state" / "exitmgr" / "schema-rejections")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise RecoveryError("unsafe rejection receipt directory")
    os.chmod(root, 0o700)
    identifier = uuid.uuid4().hex
    safe_runtime = {key: runtime_before[key] for key in (
        "artifact_id", "model_id", "artifact_manifest_sha256", "runtime_receipt_sha256",
        "runtime_contract_sha256", "started_unix", "startup_nonce")
        if isinstance(runtime_before, dict) and key in runtime_before}
    summary = {key: value for key, value in analysis.items()
               if key not in ("canonical", "editable")}
    record = {"schema": "native_schema_rejection.v1", "recorded_unix": time.time(),
              "attempt": attempt, "outcome": outcome, "request_sha256": digest(body),
              "response_sha256": digest(result), "runtime_before": safe_runtime,
              "runtime_before_sha256": digest(runtime_before), "diagnostic": summary}
    choices = result.get("choices", []) if isinstance(result, dict) else []
    record["finish_reason"] = choices[0].get("finish_reason") if len(choices) == 1 else None
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) <= 1024 * 1024:
        record["rejected_response"] = result
    else:
        record["rejected_response_omitted"] = "exceeds private receipt 1MiB bound; hash retained"
    raw = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode()
    path = root / (identifier + ".json")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    return {"id": identifier, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise RecoveryError("native editorial recovery deadline exhausted")
    return value


def read_response(response, deadline):
    """Public API contract; production-derived narrative omitted."""
    chunks = []
    read = getattr(response, "read1", None) or response.read
    while True:
        wait = remaining(deadline)
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(wait)
        chunk = read(65536)
        remaining(deadline)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        if sum(map(len, chunks)) > 4 * 1024 * 1024:
            raise RecoveryError("native response exceeds 4MiB read bound")


def recover(body, rejected, schema, parse_stage_a, validate, post_once, deadline,
            runtime_before=None, directory=None):
    """Public API contract; production-derived narrative omitted."""
    analysis = analyze(body, rejected, schema, parse_stage_a)
    first = receipt(body, rejected, analysis, runtime_before, 0, "rejected", directory)
    violations = [item for item in analysis["lengths"]
                  if item["observed"] is None or not item["minimum"] <= item["observed"] <= item["maximum"]]
    diagnostic = json.dumps({"reason": analysis["reason"], "violations": violations},
                            separators=(",", ":"))
    if not analysis["eligible"]:
        raise RecoveryError("native schema rejection (receipt=%s): %s" % (first["id"], diagnostic))
    try:
        remaining(deadline)
        revision = copy.deepcopy(body)
        revision["messages"] = list(revision["messages"]) + [
            {"role": "assistant", "content": json.dumps(analysis["canonical"], ensure_ascii=False)},
            {"role": "user", "content": (
                "Editorial correction only. Shorten ONLY these overlength intent text fields: "
                + json.dumps(analysis["editable"]) + ". Alpha must be 1–600 characters; thesis "
                "must be 1–1200 characters. Preserve the supplied evidence, trade case and "
                "invalidation, including any folded notes. Keep every other field and value, "
                "intent count and order EXACTLY unchanged. Do not add an intent, drop an intent, "
                "or add explanatory keys. Return the complete same-schema JSON document only.")},
        ]
        corrected = post_once(revision, remaining(deadline))
        remaining(deadline)


        parsed = json.loads(corrected["choices"][0]["message"]["content"], object_pairs_hook=_unique,
                            parse_constant=_constant, parse_float=_float)
        baseline = analysis["canonical"]
        if not isinstance(parsed, dict) or set(parsed) != {"intents"}:
            raise RecoveryError("editorial response changed top-level fields")
        if len(parsed["intents"]) != len(baseline["intents"]):
            raise RecoveryError("editorial response changed intent count")
        editable = {tuple(x) for x in analysis["editable"]}
        for index, (original, revised) in enumerate(zip(baseline["intents"], parsed["intents"])):
            if set(original) != set(revised):
                raise RecoveryError("editorial response changed intent keys")
            for key, value in original.items():
                if ((index, key) not in editable and
                        (type(value) is not type(revised[key]) or value != revised[key])):
                    raise RecoveryError("editorial response changed frozen field intents[%s].%s" % (index, key))
        validate(body, corrected)
        remaining(deadline)
    except Exception as exc:
        failed = locals().get("corrected")
        if failed is not None:
            receipt(locals().get("revision", body), failed,
                    analyze(body, failed, schema, parse_stage_a), runtime_before, 1,
                    "editorial_rejected", directory)
        raise RecoveryError("native editorial attempt failed (receipt=%s): %s: %s" %
                            (first["id"], type(exc).__name__, str(exc)[:400])) from exc
    corrected["_trader_text_recovery"] = {
        "mode": "one_editorial_revision_v1", "rejected_receipt": first,
        "original_request_sha256": digest(body), "rejected_response_sha256": digest(rejected),
        "editorial_request_sha256": digest(revision), "edited_fields": analysis["editable"],
    }
    return corrected, revision
