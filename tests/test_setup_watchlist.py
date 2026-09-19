from datetime import date
from multiprocessing import get_context

import pytest

from exitmgr import setup_watchlist as sw


def _add_worker(args):
    path, symbol = args
    return sw.add_target(symbol, setup="concurrent", path=path, added="2026-08-29",
                         expires="2026-09-28")


def test_missing_watchlist_is_empty(tmp_path):
    snap = sw.read_watchlist(str(tmp_path / "missing.md"), today=date(2026, 8, 29))
    assert snap.targets == ()
    assert sw.render_for_brief(snap) == ""


def test_parser_renders_only_active_non_executable_setup_fields(tmp_path):
    path = tmp_path / "watch.md"
    path.write_text(
        "# Trader Setup Watchlist\n\n"
        "## NVDA\n"
        "- Status: active\n"
        "- Bias: bullish\n"
        "- Added: 2026-08-29\n"
        "- Setup: Strong name, but extended after the opening surge.\n"
        "- Wait for: Pullback plus stabilization.\n"
        "- Contract: NVDA 220/225C\n"
        "- Approval: approved\n\n"
        "## OLD\n"
        "- Status: archived\n"
        "- Setup: stale\n",
        encoding="utf-8")
    snap = sw.read_watchlist(str(path), today=date(2026, 8, 29))
    assert snap.symbols == ("NVDA",)
    rendered = sw.render_for_brief(snap)
    assert "NVDA" in rendered
    assert "Pullback plus stabilization" in rendered
    assert "context only; never an order or approval" in rendered
    assert "not an automatic buy" in rendered
    assert "220/225C" not in rendered
    assert "approved" not in rendered.lower().replace("never an order or approval", "")
    assert "OLD" not in rendered


def test_expired_duplicate_and_bad_expiry_are_handled_fail_soft(tmp_path):
    path = tmp_path / "watch.md"
    path.write_text(
        "## OLD\n- Status: active\n- Expires: 2026-08-28\n"
        "## NVDA\n- Status: active\n- Expires: nonsense\n"
        "## NVDA\n- Status: active\n- Setup: duplicate\n",
        encoding="utf-8")
    snap = sw.read_watchlist(str(path), today=date(2026, 8, 29))
    assert snap.symbols == ("NVDA",)
    assert any("invalid expiry" in w for w in snap.warnings)
    assert any("duplicate active" in w for w in snap.warnings)


def test_add_target_preserves_human_text_and_refreshes_active_entry(tmp_path):
    path = tmp_path / "watch.md"
    original = "# My note\n\n## NVDA\n- Status: archived\n- Setup: old setup\n"
    path.write_text(original, encoding="utf-8")
    assert sw.add_target("nvda", setup="wait now", bias="bullish", path=str(path),
                         added="2026-08-29") is True
    text = path.read_text(encoding="utf-8")
    assert text.startswith(original.rstrip())
    assert text.count("## NVDA") == 2
    snap = sw.read_watchlist(str(path), today=date(2026, 8, 29))
    assert snap.symbols == ("NVDA",)
    assert snap.targets[0].setup == "wait now"
    assert sw.add_target("NVDA", setup="refreshed", path=str(path),
                         added="2026-08-30", expires="2026-09-29") is True
    refreshed = sw.read_watchlist(str(path), today=date(2026, 8, 30))
    assert refreshed.symbols == ("NVDA",)
    assert refreshed.targets[0].setup == "refreshed"
    assert path.read_text(encoding="utf-8").startswith("# My note")


def test_written_fields_cannot_inject_markdown_targets(tmp_path):
    path = tmp_path / "watch.md"
    sw.add_target(
        "NVDA",
        setup="extended\n## AMD\n- Status: active",
        bias="bullish\n## MUTX",
        wait_for="pullback\r\n## AVGO",
        source="model\n## ARM",
        path=str(path), added="2026-08-29", expires="2026-09-28")
    snap = sw.read_watchlist(str(path), today=date(2026, 8, 29))
    assert snap.symbols == ("NVDA",)
    assert "## AMD" not in path.read_text(encoding="utf-8")


def test_concurrent_process_additions_do_not_lose_targets(tmp_path):
    path = str(tmp_path / "watch.md")
    symbols = [f"W{i:02d}" for i in range(8)]
    with get_context("fork").Pool(len(symbols)) as pool:
        assert all(pool.map(_add_worker, [(path, symbol) for symbol in symbols]))
    snap = sw.read_watchlist(path, today=date(2026, 8, 29))
    assert set(snap.symbols) == set(symbols)
    assert len(snap.symbols) == len(symbols)


def test_active_target_bound_matches_fresh_opening_research_capacity(tmp_path):
    path = str(tmp_path / "watch.md")
    for i in range(sw.MAX_TARGETS):
        sw.add_target(f"T{i:02d}", setup="bounded", path=path,
                      added="2026-08-29", expires="2026-09-28")
    assert sw.MAX_TARGETS == 35
    with pytest.raises(ValueError, match="maximum"):
        sw.add_target("EXTRA", setup="too many", path=path,
                      added="2026-08-29", expires="2026-09-28")


def test_oversize_watchlist_is_ignored(tmp_path):
    path = tmp_path / "watch.md"
    path.write_bytes(b"x" * (sw.MAX_BYTES + 1))
    snap = sw.read_watchlist(str(path), today=date(2026, 8, 29))
    assert snap.targets == ()
    assert any("exceeds" in w for w in snap.warnings)


def test_opening_universe_prioritizes_targets_without_duplicates():
    out = sw.prioritize_for_opening(
        ["SPY", "QQQ", "IWM"], ["AAPL", "NVDA", "MSFT", "AMD"],
        ["NVDA", "MUTX"], non_core_limit=4)
    assert out == ["SPY", "QQQ", "IWM", "NVDA", "MUTX", "AAPL", "MSFT"]
