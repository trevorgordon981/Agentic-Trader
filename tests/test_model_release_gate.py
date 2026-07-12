import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from exitmgr import model_release_gate
from exitmgr.risk import RiskLimits
from exitmgr.trader import ResolvedOrder, Trader


_PRODUCTION_LEDGER_READER = model_release_gate._activation_head_from_ledger


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_canonical(path, value):
    path.write_bytes((json.dumps(value, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True, allow_nan=False) + "\n").encode("ascii"))


@pytest.fixture(autouse=True)
def _isolated_activation_reader(monkeypatch):
    """Production requires a root-owned ledger; tests inject an isolated head."""
    monkeypatch.setattr(
        model_release_gate, "_activation_head_from_ledger",
        lambda path: json.loads(Path(path).read_text()))


def _model_identity(runtime):
    return {
        "schema": "trader-model-request.v1",
        "verified": True,
        "runtime": dict(runtime),
        **{key: runtime[key] for key in (
            "artifact_id", "artifact_manifest_sha256", "runtime_receipt_sha256",
            "runtime_contract_sha256", "model_realpath")},
    }


def _release_fixture(tmp_path):
    root = tmp_path.resolve()
    model = root / "m3-v3-quantized"
    model.mkdir()
    artifact = root / "artifact.json"
    quant = root / "quantization.json"
    artifact.write_text("artifact receipt\n")
    quant.write_text("quantization receipt\n")
    evaluations = {}
    for pillar in ("general_reasoning", "trading", "portfolio"):
        path = root / f"{pillar}.json"
        path.write_text(f"{pillar} frozen evaluation\n")
        path.chmod(0o444)
        evaluations[pillar] = path

    artifact_id = "m3-v3-serving-20260711"
    manifest_sha = _digest(artifact)
    runtime = {
        "runtime_backend": "custom-python-mlx",
        "runtime_schema": "pipeline-m3-runtime.v1",
        "binding_kind": "pipeline-artifact",
        "artifact_id": artifact_id,
        "artifact_manifest_sha256": manifest_sha,
        "model_realpath": str(model),
        "runtime_receipt_sha256": "b" * 64,
        "runtime_contract_sha256": "c" * 64,
        "readiness_smoke_sha256": "d" * 64,
        "startup_nonce": "startup-nonce-20260711",
    }
    noninferiority = {}
    for pillar, path in evaluations.items():
        noninferiority[pillar] = {
            "receipt_path": str(path),
            "receipt_sha256": _digest(path),
            "decision": "PASS",
            "frozen": True,
            "candidate_artifact_id": artifact_id,
            "candidate_artifact_manifest_sha256": manifest_sha,
        }
    now = datetime.now(timezone.utc).replace(microsecond=0)
    not_before = now - timedelta(minutes=1)
    expires_at = now + timedelta(days=1)
    receipt_value = {
        "schema": "alfred-model-promotion.v1",
        "promotion_id": "m3-v3-release-20260711",
        "release_sequence": 1,
        "activation_nonce": "a" * 64,
        "status": "PROMOTED",
        "not_before": not_before.isoformat().replace("+00:00", "Z"),
        "promoted_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "lineage": {
            "stage": "v3",
            "parent_bf16": {
                "artifact_id": "m3-v1-fused-abliterated-bf16",
                "artifact_manifest_sha256": "e" * 64,
                "model_tree_sha256": "f" * 64,
            },
        },
        "serving_artifact": {
            "backend": "custom-python-mlx",
            "runtime_schema": "pipeline-m3-runtime.v1",
            "binding_kind": "pipeline-artifact",
            "artifact_id": artifact_id,
            "artifact_manifest_path": str(artifact),
            "artifact_manifest_sha256": manifest_sha,
            "model_realpath": str(model),
            "runtime_receipt_sha256": "b" * 64,
            "runtime_contract_sha256": "c" * 64,
            "readiness_smoke_sha256": "d" * 64,
            "startup_nonce": "startup-nonce-20260711",
            "quantization_receipt_path": str(quant),
            "quantization_receipt_sha256": _digest(quant),
        },
        "noninferiority": noninferiority,
    }
    receipt = root / "promotion.json"
    _write_canonical(receipt, receipt_value)

    activation = root / "activation-head.json"
    _write_canonical(activation, {
        "schema": "alfred-model-activation.v1",
        "sequence": 1,
        "action": "ACTIVATE",
        "promotion_id": receipt_value["promotion_id"],
        "promotion_receipt_sha256": _digest(receipt),
        "activation_nonce": receipt_value["activation_nonce"],
        "recorded_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": receipt_value["expires_at"],
        "previous_record_sha256": "0" * 64,
    })

    key = root / "promotion_signing"
    subprocess.run(
        [shutil.which("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True)
    allowed = root / "allowed_signers"
    allowed.write_text("alfred-model-promotion " + (root / "promotion_signing.pub").read_text())
    allowed.chmod(0o444)
    subprocess.run(
        [shutil.which("ssh-keygen"), "-Y", "sign", "-f", str(key),
         "-n", "alfred-model-promotion-v1", str(receipt)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    signature = Path(str(receipt) + ".sig")
    manual_key = root / "manual-provenance.key"
    manual_key.write_text("manual-test-key-which-is-at-least-thirty-two-bytes\n")
    manual_key.chmod(0o600)
    settings = model_release_gate.ModelReleaseGateSettings(
        enabled=True, promotion_receipt=str(receipt), signature=str(signature),
        allowed_signers=str(allowed), activation_ledger=str(activation),
        manual_provenance_key=str(manual_key))
    return settings, runtime, receipt_value, evaluations


def test_disabled_gate_is_true_noop():
    result = model_release_gate.require_v3_release(
        model_release_gate.ModelReleaseGateSettings(), endpoint="not even a URL",
        runtime_snapshot=lambda _: (_ for _ in ()).throw(AssertionError("must not inspect runtime")))
    assert result == {"enabled": False}


def test_settings_absent_is_off_but_attempted_typo_fails_closed(tmp_path):
    assert not model_release_gate.settings_from_mapping({}).enabled
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="unknown"):
        model_release_gate.settings_from_mapping({"model_release_gate": {"enable": True}})
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="true or false"):
        model_release_gate.settings_from_mapping({"model_release_gate": {"enabled": "true"}})


def test_poisoned_entry_setting_blocks_without_disabling_exit_process():
    settings = model_release_gate.ModelReleaseGateSettings(
        enabled=True, configuration_error="bad activation")
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="bad activation"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            runtime_snapshot=lambda _: (_ for _ in ()).throw(AssertionError("no health read")))


def test_valid_signed_v3_release_binds_exact_runtime(tmp_path):
    settings, runtime, _, _ = _release_fixture(tmp_path)
    evidence = model_release_gate.require_v3_release(
        settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
        decision_identity=_model_identity(runtime), runtime_snapshot=lambda _: runtime)
    assert evidence["enabled"] is True
    assert evidence["stage"] == "v3"
    assert evidence["artifact_id"] == runtime["artifact_id"]
    assert evidence["runtime_receipt_sha256"] == runtime["runtime_receipt_sha256"]


def test_tampered_receipt_fails_signature_before_entry(tmp_path):
    settings, runtime, value, _ = _release_fixture(tmp_path)
    value["status"] = "PROMOTED "
    _write_canonical(Path(settings.promotion_receipt), value)
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="signature"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            runtime_snapshot=lambda _: runtime)


@pytest.mark.parametrize("mutation,match", [
    (lambda value: value.update(status="CANDIDATE"), "not PROMOTED"),
    (lambda value: value["lineage"].update(stage="v2"), "not v3"),
    (lambda value: value["serving_artifact"].update(backend="vmlx"), "custom Python"),
    (lambda value: value["noninferiority"]["trading"].update(decision="FAIL"),
     "not frozen PASS"),
])
def test_semantic_downgrades_fail_closed(tmp_path, mutation, match):
    settings, runtime, value, _ = _release_fixture(tmp_path)
    mutation(value)
    _write_canonical(Path(settings.promotion_receipt), value)
    with pytest.raises(model_release_gate.ModelReleaseGateError, match=match):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            runtime_snapshot=lambda _: runtime,
            signature_verifier=lambda *_: None)


def test_noncanonical_or_duplicate_receipt_fails_closed(tmp_path):
    settings, runtime, _, _ = _release_fixture(tmp_path)
    Path(settings.promotion_receipt).write_text('{"schema":"x", "schema":"x"}\n')
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="duplicate"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            runtime_snapshot=lambda _: runtime,
            signature_verifier=lambda *_: None)


@pytest.mark.parametrize("key", [
    "artifact_id", "artifact_manifest_sha256", "model_realpath",
    "runtime_receipt_sha256", "runtime_contract_sha256", "readiness_smoke_sha256",
    "binding_kind", "runtime_backend", "runtime_schema", "startup_nonce",
])
def test_every_active_runtime_binding_mismatch_blocks(tmp_path, key):
    settings, runtime, _, _ = _release_fixture(tmp_path)
    changed = dict(runtime)
    changed[key] = "other"
    with pytest.raises(model_release_gate.ModelReleaseGateError, match=key):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            runtime_snapshot=lambda _: changed)


def test_model_name_is_irrelevant_but_captured_decision_runtime_must_match(tmp_path):
    settings, runtime, _, _ = _release_fixture(tmp_path)
    identity = _model_identity(runtime)
    evidence = model_release_gate.require_v3_release(
        settings, endpoint="http://localhost:8082/a/model/name/is/not/trusted",
        decision_identity=identity, runtime_snapshot=lambda _: runtime)
    assert evidence["enabled"]
    identity["runtime"]["runtime_receipt_sha256"] = "0" * 64
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="decision"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://localhost:8082/v1/chat/completions",
            decision_identity=identity, runtime_snapshot=lambda _: runtime)


def test_model_entry_missing_or_unverified_identity_fails_closed(tmp_path):
    settings, runtime, _, _ = _release_fixture(tmp_path)
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="missing runtime identity"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            runtime_snapshot=lambda _: runtime)
    identity = _model_identity(runtime)
    identity["verified"] = False
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="verified request"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            decision_identity=identity, runtime_snapshot=lambda _: runtime)


def test_activation_head_prevents_replay_and_revocation_is_immediate(tmp_path):
    settings, runtime, value, _ = _release_fixture(tmp_path)
    identity = _model_identity(runtime)
    head_path = Path(settings.activation_ledger)
    head = json.loads(head_path.read_text())
    head.update(sequence=2, promotion_id="newer-release",
                promotion_receipt_sha256="9" * 64,
                activation_nonce="8" * 64)
    _write_canonical(head_path, head)
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="ledger head"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            decision_identity=identity, runtime_snapshot=lambda _: runtime)

    head.update(action="REVOKE", promotion_id=value["promotion_id"],
                promotion_receipt_sha256=_digest(Path(settings.promotion_receipt)),
                activation_nonce=value["activation_nonce"])
    _write_canonical(head_path, head)
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="revoked"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            decision_identity=identity, runtime_snapshot=lambda _: runtime)


def test_root_ledger_reader_enforces_contiguous_hash_chained_records(tmp_path):
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    previous = "0" * 64
    for sequence, action in ((1, "ACTIVATE"), (2, "REVOKE")):
        record = {
            "schema": "alfred-model-activation.v1",
            "sequence": sequence,
            "action": action,
            "promotion_id": "release-one",
            "promotion_receipt_sha256": "1" * 64,
            "activation_nonce": "2" * 64,
            "recorded_at": (now + timedelta(seconds=sequence)).isoformat().replace(
                "+00:00", "Z"),
            "expires_at": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "previous_record_sha256": previous,
        }
        scratch = ledger / "scratch"
        _write_canonical(scratch, record)
        digest = _digest(scratch)
        final = ledger / f"{sequence:020d}-{action.lower()}-{digest}.json"
        scratch.rename(final)
        final.chmod(0o444)
        previous = digest
    head = _PRODUCTION_LEDGER_READER(str(ledger), required_uid=os.geteuid())
    assert head["sequence"] == 2
    assert head["action"] == "REVOKE"

    last = sorted(ledger.iterdir())[-1]
    bad = json.loads(last.read_text())
    bad["previous_record_sha256"] = "f" * 64
    last.chmod(0o600)
    last.unlink()
    scratch = ledger / "scratch"
    _write_canonical(scratch, bad)
    bad_digest = _digest(scratch)
    scratch.rename(ledger / f"{2:020d}-revoke-{bad_digest}.json")
    (ledger / f"{2:020d}-revoke-{bad_digest}.json").chmod(0o444)
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="hash chain"):
        _PRODUCTION_LEDGER_READER(str(ledger), required_uid=os.geteuid())

def test_signed_release_expiry_is_enforced(tmp_path):
    settings, runtime, value, _ = _release_fixture(tmp_path)
    after_expiry = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00")) \
        + timedelta(seconds=1)
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="expired"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            decision_identity=_model_identity(runtime), runtime_snapshot=lambda _: runtime,
            now=after_expiry)


def test_manual_entry_requires_short_lived_hmac_bound_to_exact_order(tmp_path):
    settings, runtime, _, _ = _release_fixture(tmp_path)
    intent = model_release_gate.manual_order_intent(
        decision_id="decision-" + "a" * 32, symbol="SPY", right="C",
        expiry="20270115", strike=600.0, quantity=1, limit_price=1.10,
        contract_id=11)
    proof = model_release_gate.issue_manual_decision_proof(
        settings, intent=intent, approved=True)
    evidence = model_release_gate.require_v3_release(
        settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
        decision_origin="manual", manual_proof=proof, manual_intent=intent,
        runtime_snapshot=lambda _: runtime)
    assert evidence["enabled"]
    changed = dict(intent, quantity=2)
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="another order"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            decision_origin="manual", manual_proof=proof, manual_intent=changed,
            runtime_snapshot=lambda _: runtime)


def test_final_revalidation_blocks_runtime_swap_after_approval(tmp_path):
    settings, runtime, _, _ = _release_fixture(tmp_path)
    identity = _model_identity(runtime)
    evidence = model_release_gate.require_v3_release(
        settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
        decision_identity=identity, runtime_snapshot=lambda _: runtime)
    swapped = dict(runtime, startup_nonce="swapped-startup")
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="startup_nonce"):
        model_release_gate.revalidate_v3_release(
            evidence, settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            decision_identity=identity, runtime_snapshot=lambda _: swapped)


def test_frozen_noninferiority_file_must_remain_read_only_and_unchanged(tmp_path):
    settings, runtime, _, evaluations = _release_fixture(tmp_path)
    evaluations["portfolio"].chmod(0o600)
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="frozen read-only"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://127.0.0.1:8082/v1/chat/completions",
            runtime_snapshot=lambda _: runtime)


def test_nonlocal_runtime_endpoint_is_refused_even_with_signed_receipt(tmp_path):
    settings, runtime, _, _ = _release_fixture(tmp_path)
    with pytest.raises(model_release_gate.ModelReleaseGateError, match="local"):
        model_release_gate.require_v3_release(
            settings, endpoint="http://model.example/v1/chat/completions",
            runtime_snapshot=lambda _: runtime)


def test_read_only_preflight_fails_when_gate_is_not_enabled(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("trading:\n  llm_endpoint: http://127.0.0.1:8082/v1/chat/completions\n")
    script = Path(__file__).resolve().parents[1] / "ops" / "verify_v3_model_release.py"
    result = subprocess.run(
        [sys.executable, str(script), "--config", str(config)],
        cwd=str(script.parents[1]), text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False)
    assert result.returncode == 2
    assert "disabled" in result.stderr


@pytest.mark.asyncio
async def test_trader_gate_failure_is_before_place_order(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("kill_switch:\n  path: ./KILL_SWITCH\n")
    ib = SimpleNamespace(placeOrder=Mock())
    conn = SimpleNamespace(ib=ib, create_combo_contract=Mock())
    enabled = model_release_gate.ModelReleaseGateSettings(
        True, str(tmp_path / "receipt"), str(tmp_path / "sig"), str(tmp_path / "allowed"))
    trader = Trader(
        ib_conn=conn, exit_manager=SimpleNamespace(), limits=RiskLimits(), approved_names={"SPY"},
        endpoint="http://127.0.0.1:8082/v1/chat/completions", model="untrusted-label",
        slack_token="", slack_channel="", approver_ids=set(),
        baseline_path=str(tmp_path / "base"), audit_path=str(tmp_path / "audit.jsonl"),
        config_path=str(config), kill_switch_path="./KILL_SWITCH",
        trading_down_path=str(tmp_path / "TRADING_DOWN"),
        model_release_gate_settings=enabled)
    resolved = ResolvedOrder(
        "SPY", "C", "20270115", 600.0, 1, 1.05, SimpleNamespace(conId=11),
        entry_bid=1.00, entry_ask=1.10, quote_observed_at=time.monotonic(),
        decision_id="decision-" + "a" * 32)
    monkeypatch.setattr(
        model_release_gate, "require_v3_release",
        lambda *a, **k: (_ for _ in ()).throw(
            model_release_gate.ModelReleaseGateError("not promoted")))
    with pytest.raises(RuntimeError, match="model release gate"):
        await trader._submit_order(resolved)
    ib.placeOrder.assert_not_called()


def test_all_direct_new_entry_call_sites_have_release_gate_and_exits_do_not():
    root = Path(__file__).resolve().parents[1]
    for rel in ("daily_recommend.py", "place_trade.py", "exitmgr/trader.py"):
        lines = (root / rel).read_text().splitlines()
        for idx, line in enumerate(lines):
            if ".placeOrder(" not in line:
                continue
            context = "\n".join(lines[max(0, idx - 70):idx + 1])
            assert "require_v3_release" in context, f"ungated new entry in {rel}:{idx + 1}"
            adjacent = "\n".join(lines[max(0, idx - 12):idx + 1])
            assert "revalidate_v3_release" in adjacent, \
                f"no immediate runtime revalidation in {rel}:{idx + 1}"
    # Protective SELL-to-close placement is intentionally independent: a failed
    # release proof must never suppress de-risking.
    assert "model_release_gate" not in (root / "exitmgr" / "order.py").read_text()
    assert "model_release_gate" not in (root / "exitmgr" / "manager.py").read_text()
    runner = (root / "run_trader.py").read_text()
    assert "Protective exits remain armed" in runner
    # The shared connection wrapper is used only by explicit de-risking callers;
    # no hidden BUY-entry path may bypass the three direct seams above.
    wrapper_callers = set()
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root))
        if rel.startswith("tests/"):
            continue
        if ".place_order(" in path.read_text():
            wrapper_callers.add(rel)
    assert wrapper_callers == {"close_symbol.py", "liquidate.py", "exitmgr/order.py"}
    for rel in ("daily_recommend.py", "exitmgr/trader.py"):
        assert 'decision_origin="model"' in (root / rel).read_text()
        assert "preflight_v3_release" not in (root / rel).read_text()
    manual = (root / "place_trade.py").read_text()
    assert 'decision_origin="manual"' in manual
    assert "issue_manual_decision_proof" in manual
