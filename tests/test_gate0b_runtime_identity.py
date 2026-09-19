"""Public API contract; production-derived narrative omitted."""
import json
import subprocess

import pytest

from exitmgr.config import TRADING_DEFAULTS, load_config
from exitmgr.runtime_identity import (
    RuntimeIdentityError, execution_config_dict, freeze_runtime_identity, git_head,
    resolved_config_dict,
    resolved_config_sha256,
)


def _config(tmp_path, name, text):
    path = tmp_path / name
    caps = "" if "caps:" in text else (
        "caps:\n  max_orders_per_day: 20\n  max_notional_per_day: 1000\n"
    )
    path.write_text(text + caps + "construction:\n  max_entry_spread_pct: 25.0\n")
    return load_config(str(path))


def test_yaml_format_comments_and_key_order_do_not_change_policy_hash(tmp_path):
    left = _config(
        tmp_path, "left.yaml",
        "# comment\nib:\n  port: 4001\ncaps:\n  max_orders_per_day: 20\n"
        "  max_notional_per_day: 1000\n",
    )
    right = _config(
        tmp_path, "right.yaml",
        "caps: {max_notional_per_day: 1000, max_orders_per_day: 20}\n"
        "ib: {port: 4001}\n",
    )
    assert resolved_config_sha256(left) == resolved_config_sha256(right)


def test_any_effective_policy_change_moves_the_hash(tmp_path):
    left = _config(tmp_path, "left.yaml", "trading: {max_concurrent: 8}\n")
    right = _config(tmp_path, "right.yaml", "trading: {max_concurrent: 7}\n")
    assert resolved_config_sha256(left) != resolved_config_sha256(right)


def test_payload_contains_every_section_and_registered_trading_key(tmp_path):
    payload = resolved_config_dict(_config(tmp_path, "config.yaml", ""))
    for section in ("ib", "journal", "state", "kill_switch", "loop", "scope", "caps",
                    "rules", "construction"):
        assert section in payload
    missing = [key for key, _default in TRADING_DEFAULTS if key not in payload]
    assert missing == []


def test_execution_mapping_is_built_from_the_resolved_typed_policy(tmp_path):
    cfg = _config(tmp_path, "config.yaml", "trading: {cash_buffer_pct: 0.10}\n")
    execution = execution_config_dict(cfg)
    assert set(execution) == {
        "ib", "journal", "state", "kill_switch", "loop", "scope", "caps", "rules",
        "construction", "trading",
    }
    assert execution["trading"]["cash_buffer_pct"] == 0.10
    assert execution["trading"]["max_concurrent"] == dict(TRADING_DEFAULTS)["max_concurrent"]
    assert execution["rules"]["stop_pct"] == cfg.rules.stop_pct


def test_daily_and_manual_routes_do_not_reparse_yaml_after_identity_freeze():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    for route in ("daily_recommend.py", "place_trade.py"):
        source = (root / route).read_text()
        run_body = source[source.index("async def run(args):"):]
        assert "yaml.safe_load" not in run_body
        assert "execution_config_dict" in run_body


def test_runtime_invocation_flags_are_not_mislabeled_as_policy(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "caps:\n  max_orders_per_day: 20\n  max_notional_per_day: 1000\n"
        "construction:\n  max_entry_spread_pct: 25.0\n"
    )
    dry = load_config(str(path), arm=False, loop=False)
    live = load_config(str(path), arm=True, loop=True)
    assert resolved_config_sha256(dry) == resolved_config_sha256(live)


def test_git_head_is_the_exact_lowercase_commit_of_this_checkout():
    expected = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip().lower()
    assert git_head() == expected
    assert len(expected) == 40 and expected == expected.lower()


def test_unresolvable_code_identity_is_fatal(tmp_path):
    with pytest.raises(RuntimeIdentityError, match="Git HEAD"):
        git_head(tmp_path)


def test_production_identity_refuses_dirty_or_untracked_executable_source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    source = repo / "worker.py"
    source.write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "worker.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    assert len(git_head(repo, require_clean_code=True)) == 40

    source.write_text("VALUE = 2\n")
    with pytest.raises(RuntimeIdentityError, match="does not match Git HEAD"):
        git_head(repo, require_clean_code=True)
    source.write_text("VALUE = 1\n")
    (repo / "untracked.py").write_text("VALUE = 3\n")
    with pytest.raises(RuntimeIdentityError, match="untracked.py"):
        git_head(repo, require_clean_code=True)


def test_frozen_pair_has_exact_shapes(tmp_path):
    identity = freeze_runtime_identity(_config(tmp_path, "config.yaml", ""))
    assert len(identity.code_version) == 40
    assert len(identity.policy_version) == 64
    int(identity.code_version, 16)
    int(identity.policy_version, 16)


def test_noncanonical_values_fail_instead_of_stringifying(tmp_path):
    cfg = _config(tmp_path, "config.yaml", "")
    cfg.approved_names = [object()]
    with pytest.raises(RuntimeIdentityError, match="unsupported"):
        resolved_config_sha256(cfg)
