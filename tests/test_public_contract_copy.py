"""Public API contract; production-derived narrative omitted."""

from pathlib import Path

import exitmgr.trader as trader
import run_trader


ROOT = Path(__file__).resolve().parents[1]


def test_module_docs_do_not_promise_human_approval_for_every_armed_entry():
    combined = "\n".join((run_trader.__doc__ or "", trader.__doc__ or "")).lower()
    assert "explicit approval came back" not in combined
    assert "slack approval per entry" not in combined
    assert "auto_approve_within_gates" in combined
    assert "no approval is awaited" in combined


def test_readme_does_not_delegate_truth_to_code_over_stale_docs():
    text = (ROOT / "README.md").read_text().lower()
    assert "believe the code" not in text
    assert "text is **stale**" not in text
    assert "lane-specific authority rules" in text


def test_daily_slate_docs_match_its_autonomous_authority_path():
    module_doc = (run_trader.__doc__ or "").lower()
    readme = (ROOT / "README.md").read_text().lower()
    source = (ROOT / "daily_recommend.py").read_text().lower()

    for text in (module_doc, readme):
        assert "`daily_recommend.py`: always a human" not in text
        assert "daily_recommend.py` contains no reference" not in text
        assert "model-authored" in text
        assert "no-tap" in text

    assert "auto_approve_within_gates" in source
    assert "entrysubmissionauthority.autonomous()" in source
    assert "no approval is being awaited" in source


def test_docs_do_not_claim_autonomous_refresh_always_needs_human_reapproval():
    combined = "\n".join(
        ((ROOT / "README.md").read_text(), trader.__doc__ or "")
    ).lower()
    assert "reapproval of any changed order" not in combined
    assert "every materially changed order require fresh human authority" not in combined
    assert "refreshed exact order clears every hard gate" in combined
