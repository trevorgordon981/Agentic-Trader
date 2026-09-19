"""Public API contract; production-derived narrative omitted."""
import ast
import asyncio
import inspect
import json
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from exitmgr.config import Config, RulesConfig, ScaleOutConfig, TrailingConfig
from exitmgr.connection import OrderData, PositionData
from exitmgr.manager import ExitManager


CON = 3001001
OTHER = 884401
JOURNAL = {"ts": "2026-08-01T14:00:00+00:00", "contract_id": CON, "symbol": "SYMA",
           "right": "P", "strike": 11.0, "expiry": "20261231", "quantity": 6,
           "debit": 426.0, "conviction": 7}


def _manager(tmp_path, *, channel=""):
    cfg = Config()
    cfg.dry_run = False
    cfg.loop_mode = False
    cfg.journal.path = str(tmp_path / "trades.log")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.kill_switch.path = str(tmp_path / "KILL")
    cfg.audit_path = str(tmp_path / "audit.jsonl")
    cfg.manage_positions = False
    cfg.alerts_channel = channel
    cfg.error_channel = ""
    cfg.rules = RulesConfig(profit_target_pct=300.0, stop_pct=30.0, time_stop_days=0,
                            trailing=TrailingConfig(enabled=False),
                            scale_out=ScaleOutConfig(enabled=False))
    (tmp_path / "trades.log").write_text(json.dumps(JOURNAL) + "\n")
    return ExitManager(cfg)


def _book(**over):
    d = {"order_id": 101, "remaining": 6, "perm_id": 0, "client_id": None,
         "order_ref": None, "status": "Submitted",
         "order_ids": [101, 205], "order_count": 2}
    d.update(over)
    return {CON: d}





def test_two_resting_closes_on_one_contract_are_found(tmp_path):
    mgr = _manager(tmp_path)
    found = mgr._duplicate_resting_closes(_book())
    assert len(found) == 1
    sym, cid, ids, count, remaining = found[0]
    assert cid == CON
    assert sorted(ids) == [101, 205]
    assert count == 2

    assert remaining == 6


def test_one_resting_close_is_silence(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _manager(tmp_path)
    assert mgr._duplicate_resting_closes(_book(order_ids=[101], order_count=1)) == []


def test_an_empty_book_is_silence(tmp_path):
    assert _manager(tmp_path)._duplicate_resting_closes({}) == []


def test_the_symbol_comes_from_the_live_position_when_there_is_one(tmp_path):
    mgr = _manager(tmp_path)
    pos = {CON: PositionData(con_id=CON, symbol="SYMA", right="P", quantity=6,
                             avg_cost=0.71, expiry="20261231")}
    assert mgr._duplicate_resting_closes(_book(), pos)[0][0] == "SYMA"


def test_the_symbol_falls_back_to_the_journal_then_to_a_placeholder(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _manager(tmp_path)
    assert mgr._duplicate_resting_closes(_book(), {})[0][0] == "SYMA"
    mgr._journal_entries = {}
    assert mgr._duplicate_resting_closes(_book(), {})[0][0] == "?"


@pytest.mark.parametrize("count", [None, 0, 1, "2", True])
def test_a_missing_or_malformed_count_never_fires_and_never_raises(tmp_path, count):
    """Public API contract; production-derived narrative omitted."""
    mgr = _manager(tmp_path)
    assert mgr._duplicate_resting_closes(_book(order_count=count)) == []


def test_an_unreadable_order_view_yields_nothing_rather_than_raising(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.connection import OrderView
    mgr = _manager(tmp_path)
    assert mgr._duplicate_resting_closes(OrderView.unknown("timeout")) == []


def test_a_book_entry_that_is_not_a_dict_is_skipped(tmp_path):
    mgr = _manager(tmp_path)
    assert mgr._duplicate_resting_closes({CON: "nonsense", OTHER: None}) == []





def _capture_slack(monkeypatch):
    from exitmgr import alerting

    posted = []

    def _fake_post(text, channel_id, *, tok=None, label="", fallback_channel=None,
                   timeout=10, dedup=True, dedup_key=None, **kwargs):
        if not channel_id:
            return False
        posted.append({"text": text, "channel": channel_id})
        return True

    monkeypatch.setattr(alerting, "post", _fake_post)
    monkeypatch.setattr(alerting, "alerts_channel", lambda: "")
    monkeypatch.setattr(alerting, "error_channel", lambda: "")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    return posted


def test_the_message_names_the_con_id_and_every_order_id(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    posted = _capture_slack(monkeypatch)
    mgr = _manager(tmp_path, channel="#alerts")
    mgr._post_duplicate_close_alert(mgr._duplicate_resting_closes(_book()))

    assert len(posted) == 1
    text = posted[0]["text"]
    assert posted[0]["channel"] == "#alerts"
    assert str(CON) in text
    assert "101" in text and "205" in text
    assert "SYMA" in text

    assert "cancel" in text.lower()


def test_it_is_UNTHROTTLED_and_says_so_every_cycle_the_condition_holds(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    posted = _capture_slack(monkeypatch)
    mgr = _manager(tmp_path, channel="#alerts")
    items = mgr._duplicate_resting_closes(_book())
    for _ in range(3):
        mgr._post_duplicate_close_alert(items)
    assert len(posted) == 3


def test_it_routes_through_the_same_unthrottled_path_the_withheld_stops_alarm_uses(
        tmp_path, monkeypatch):
    seen = []
    mgr = _manager(tmp_path, channel="#alerts")
    monkeypatch.setattr(mgr, "_post_unthrottled_alert",
                        lambda body, what: seen.append((body, what)))
    mgr._post_duplicate_close_alert(mgr._duplicate_resting_closes(_book()))
    mgr._post_stops_withheld_alert([("SYMA", CON, "stop")])
    assert [w for _b, w in seen] == ["duplicate-resting-close", "stops-withheld"]


def test_nothing_is_posted_with_no_items_and_no_channel(tmp_path, monkeypatch):
    posted = _capture_slack(monkeypatch)
    _manager(tmp_path, channel="#alerts")._post_duplicate_close_alert([])
    _manager(tmp_path, channel="")._post_duplicate_close_alert(
        [("SYMA", CON, (101, 205), 2, 6)])
    assert posted == []


def test_a_slack_failure_never_raises_into_the_cycle(tmp_path, monkeypatch):
    from exitmgr import alerting

    def _boom(*args, **kwargs):
        raise OSError("slack is down")

    monkeypatch.setattr(alerting, "post", _boom)
    monkeypatch.setattr(alerting, "error_channel", lambda: "")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    mgr = _manager(tmp_path, channel="#alerts")
    mgr._post_duplicate_close_alert(mgr._duplicate_resting_closes(_book()))





def _calls_named(fn):
    """Public API contract; production-derived narrative omitted."""
    tree = ast.parse(inspect.getsource(fn).lstrip())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


@pytest.mark.parametrize("meth", ["_duplicate_resting_closes", "_post_duplicate_close_alert"])
def test_the_new_code_cancels_nothing_and_touches_no_broker(meth):
    """Public API contract; production-derived narrative omitted."""
    fn = getattr(ExitManager, meth)
    called = _calls_named(fn)
    assert not [n for n in called if "cancel" in n.lower()], sorted(called)
    src = inspect.getsource(fn)
    assert "ib_conn" not in src and "order_manager" not in src
    assert "place_" not in src
    assert not inspect.iscoroutinefunction(fn), (
        "a detector that cannot await cannot place or cancel anything")


def test_the_repository_still_has_no_cancel_machinery_at_all():
    """Public API contract; production-derived narrative omitted."""
    repo = Path(__file__).resolve().parents[1]
    hits = []
    for p in sorted((repo / "exitmgr").glob("*.py")):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if "cancelOrder" in line or line.strip().startswith("def cancel"):
                hits.append("%s:%d" % (p.name, i))
    assert not hits, hits





def _wire(mgr, *, quotes=None):
    pos = {CON: PositionData(con_id=CON, symbol="SYMA", right="P", quantity=6,
                             avg_cost=0.71, expiry="20261231")}
    mgr.ib_conn.get_positions = AsyncMock(return_value=pos)
    mgr.ib_conn.fetch_quotes = AsyncMock(return_value=quotes or {})
    mgr.ib_conn.ib = MagicMock()
    mgr.ib_conn.ib.trades.return_value = []
    mgr.ib_conn.ib.portfolio = lambda: []
    mgr.ib_conn.ib.reqCompletedOrdersAsync = AsyncMock(return_value=[])
    mgr.ib_conn.ib.reqExecutionsAsync = AsyncMock(return_value=[])
    mgr._spot_price = AsyncMock(return_value=None)
    return mgr


def _open_orders(second):
    """Public API contract; production-derived narrative omitted."""
    calls = {"n": 0}

    async def _go(short_leg_con_ids=None):
        calls["n"] += 1
        return {} if calls["n"] == 1 else second

    return _go


@pytest.mark.asyncio
async def test_a_duplicated_book_alerts_once_through_a_whole_cycle(tmp_path):
    mgr = _wire(_manager(tmp_path))
    mgr.ib_conn.get_open_orders = _open_orders(
        {CON: OrderData(con_id=CON, order_id=101, remaining=6,
                        order_ids=(101, 205), order_count=2)})
    fired = []
    mgr._post_duplicate_close_alert = lambda items: fired.append(items)
    place = AsyncMock()
    mgr.order_manager.place_close_order = place

    await mgr.run_cycle(dry_run=False)

    assert len(fired) == 1, "a duplicate resting close must never be silent"
    sym, cid, ids, count, _rem = fired[0][0]
    assert (sym, cid, sorted(ids), count) == ("SYMA", CON, [101, 205], 2)
    place.assert_not_awaited()


@pytest.mark.asyncio
async def test_NEGATIVE_CONTROL_a_single_resting_close_alerts_nothing(tmp_path):
    mgr = _wire(_manager(tmp_path))
    mgr.ib_conn.get_open_orders = _open_orders(
        {CON: OrderData(con_id=CON, order_id=101, remaining=6,
                        order_ids=(101,), order_count=1)})
    fired = []
    mgr._post_duplicate_close_alert = lambda items: fired.append(items)

    await mgr.run_cycle(dry_run=False)

    assert fired == []


@pytest.mark.asyncio
async def test_an_unreadable_book_alerts_nothing(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire(_manager(tmp_path))
    calls = {"n": 0}

    async def _seq(short_leg_con_ids=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {}
        raise asyncio.TimeoutError()

    mgr.ib_conn.get_open_orders = _seq
    fired = []
    mgr._post_duplicate_close_alert = lambda items: fired.append(items)
    mgr._post_stops_withheld_alert = lambda items, reason=None: None

    await mgr.run_cycle(dry_run=False)

    assert fired == []


@pytest.mark.asyncio
async def test_an_alert_failure_never_breaks_the_cycle(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    mgr = _wire(_manager(tmp_path))
    mgr.ib_conn.get_open_orders = _open_orders(
        {CON: OrderData(con_id=CON, order_id=101, remaining=6,
                        order_ids=(101, 205), order_count=2)})

    def _boom(items):
        raise RuntimeError("alert exploded")

    mgr._post_duplicate_close_alert = _boom
    await mgr.run_cycle(dry_run=False)
    assert mgr.state_manager.state.last_cycle is not None





def _dict_key_sets(path, must_contain):
    """Public API contract; production-derived narrative omitted."""
    tree = ast.parse(Path(path).read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if must_contain <= keys:
            out.append((node.lineno, keys))
    return out


def test_every_hand_rebuild_of_the_broker_book_keeps_its_multiplicity():
    """Public API contract; production-derived narrative omitted."""
    repo = Path(__file__).resolve().parents[1]
    canonical = _dict_key_sets(repo / "exitmgr" / "connection.py",
                               {"order_id", "remaining", "perm_id", "order_count"})
    assert len(canonical) == 1, "as_state_dicts is no longer the single canonical shape"
    want = canonical[0][1]

    rebuilds = _dict_key_sets(repo / "exitmgr" / "manager.py",
                              {"order_id", "remaining", "perm_id"})
    assert rebuilds, "the hand-rebuilds vanished -- if that is deliberate, delete this test"
    bad = [(ln, sorted(want - keys)) for ln, keys in rebuilds if want - keys]
    assert not bad, (
        "\n\nA HAND-REBUILT OPEN-ORDER MAP IS MISSING KEYS as_state_dicts SUPPLIES:\n\n"
        + "\n".join("  exitmgr/manager.py:%d  missing %s" % (ln, ks) for ln, ks in bad)
        + "\n\nThe rebuild REPLACES the canonical map for the rest of the cycle, so anything it\n"
          "drops is invisible from that line on -- including whether a contract is carrying two\n"
          "resting closes.\n")
