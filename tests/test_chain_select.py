"""Unit tests for the option-chain selection helpers (strike-window + SMART-exchange fix, 2026-06-18)."""
from types import SimpleNamespace
from exitmgr.ibkr import pick_chain, strikes_near


def _p(exch, tc, strikes):
    return SimpleNamespace(exchange=exch, tradingClass=tc, strikes=set(strikes), expirations={"20260702"})


def test_pick_chain_prefers_smart():
    # mirrors real RKLB: MIAX is params[0], SMART buried later
    params = [_p("MIAX", "RKLB", [1, 2, 3]), _p("EDGX", "RKLB", [1, 2, 3]), _p("SMART", "RKLB", [1, 2, 3])]
    assert pick_chain(params, "RKLB").exchange == "SMART"


def test_pick_chain_prefers_standard_trading_class():
    params = [_p("SMART", "RKLB1", [1, 2]), _p("SMART", "RKLB", [1, 2, 3])]  # adjusted class first
    assert pick_chain(params, "RKLB").tradingClass == "RKLB"


def test_pick_chain_fallback_no_smart():
    params = [_p("MIAX", "RKLB", [1, 2, 3])]
    assert pick_chain(params, "RKLB").exchange == "MIAX"


def test_pick_chain_empty():
    assert pick_chain([], "X") is None


def test_strikes_near_windows_around_spot_drops_stale():
    # stale adjusted strikes (QQQ-style .78) far below spot + clean near-spot strikes
    chain = [174.78, 179.78, 184.78] + [float(x) for x in range(500, 701, 5)]
    near = strikes_near(chain, 580.0, per_side=5)
    assert len(near) == 10
    assert 174.78 not in near and 179.78 not in near
    assert all(k >= 500 for k in near)


def test_strikes_near_no_ref_returns_full_chain():
    chain = [1.0, 2.0, 3.0]
    assert strikes_near(chain, None) == [1.0, 2.0, 3.0]
    assert strikes_near(chain, float("nan")) == [1.0, 2.0, 3.0]
    assert strikes_near(chain, 0) == [1.0, 2.0, 3.0]


def test_strikes_near_spot_below_chain():
    assert strikes_near([10, 20, 30], 1.0, per_side=2) == [10.0, 20.0]


def test_strikes_near_empty():
    assert strikes_near([], 100.0) == []
