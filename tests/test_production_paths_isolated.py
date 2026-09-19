"""Public API contract; production-derived narrative omitted."""
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]



WRITE_PATHS = [
    ("exitmgr/flex_ingest.py", "EXITMGR_FLEX_ARCHIVE"),
    ("exitmgr/alert_throttle.py", "EXITMGR_ALERT_THROTTLE"),
    ("exitmgr/shadow.py", "EXITMGR_SHADOW_LOG"),
    ("exitmgr/connection.py", "EXITMGR_LINK_ROTATIONS_PATH"),
    ("exitmgr/enrichment.py", "EXITMGR_ENRICH_CACHE_DIR"),
    ("exitmgr/entry_reservation.py", "EXITMGR_ENTRY_RESERVATIONS"),
]


@pytest.mark.parametrize("rel,env", WRITE_PATHS)
def test_the_path_is_redirectable(rel, env):
    src = (REPO / rel).read_text()
    assert env in src, (
        "%s writes under $HOME but never consults %s -- a test that exercises it writes to "
        "production. This is how 428 test fixtures ended up in the Flex archive." % (rel, env))


@pytest.mark.parametrize("rel,env", WRITE_PATHS)
def test_the_suite_is_actually_redirected(rel, env):
    val = os.environ.get(env)
    assert val, ("%s is unset -- %s is redirectable but nobody redirected it, so this suite is "
                 "writing to production right now" % (env, rel))
    assert str(Path.home()) not in val or "pytest" in val or "/tmp" in val or "/var/folders" in val, (
        "%s points at %r, which is not a temp location" % (env, val))


def test_conftest_sets_every_declared_override():
    """Public API contract; production-derived narrative omitted."""
    conf = (REPO / "tests" / "conftest.py").read_text()
    missing = [env for _rel, env in WRITE_PATHS if env not in conf]
    assert not missing, (
        "declared in WRITE_PATHS but never set by conftest: %s -- the inventory says these are "
        "production paths and nothing is redirecting them" % missing)


def test_the_inventory_has_not_gone_stale():
    """Public API contract; production-derived narrative omitted."""
    for rel, _env in WRITE_PATHS:
        assert (REPO / rel).exists(), "%s no longer exists; update WRITE_PATHS" % rel


def test_no_new_unguarded_home_writes_in_exitmgr():
    """Public API contract; production-derived narrative omitted."""
    ALLOWED = {"~/.hermes/.env", "~/exitmgr-app", "~/exitmgr-app/config.yaml",
               "~/evaluation-harness/trading", "~/trade-capture/shadow/decisions.jsonl",
               "~/exitmgr-app/exits.log", "~/exitmgr-app/.reconcile_alert_ts",
               "~/.local/var/exitmgr/mgmt-alerted.json", "~/.cache/exitmgr-research",
               "~/.local/var/exitmgr/link-rotations.json", "~/flex-archive",
               "~/.local/var/alert-throttle.json"}
    offenders = []
    for py in sorted((REPO / "exitmgr").glob("*.py")):
        for i, line in enumerate(py.read_text().splitlines(), 1):
            for m in re.finditer(r'expanduser\(\s*"(~/[^"]+)"', line):
                path = m.group(1)
                if path in ALLOWED:
                    continue
                if "environ" in line:
                    continue
                offenders.append("%s:%d  %s" % (py.name, i, path))
    assert not offenders, (
        "NEW hardcoded $HOME path(s) with no environment override:\n  " + "\n  ".join(offenders) +
        "\n\nIf the app writes there, add an EXITMGR_* override, wire it in conftest, and add it "
        "to WRITE_PATHS. If it only reads, add it to ALLOWED with a reason.")
