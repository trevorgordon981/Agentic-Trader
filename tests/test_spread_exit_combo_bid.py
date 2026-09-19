"""Public API contract; production-derived narrative omitted."""
import pytest

from exitmgr.order import OrderManager


class _Recorder:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self):
        self.calls = []

    def create_limit_order(self, action, qty, px, *a, **k):
        self.calls.append(("LMT", action, qty, float(px)))
        return ("LMT", float(px))

    def create_market_order(self, action, qty, *a, **k):
        self.calls.append(("MKT", action, qty, None))
        return ("MKT", None)

    def __getattr__(self, _):
        return lambda *a, **k: None


class _StateStub:
    def __getattr__(self, _):
        return lambda *a, **k: None


@pytest.fixture
def rec():
    return _Recorder()


@pytest.fixture
def om(rec):
    return OrderManager(rec, _StateStub(), exit_slippage_floor=0.50)


def kind(order):
    return order[0] if isinstance(order, tuple) else None


def price(order):
    return order[1] if isinstance(order, tuple) else None


SPREAD = {"short_con_id": 3001003, "short_strike": 10.0, "width": 1.0}


def _order(om, **kw):
    """Public API contract; production-derived narrative omitted."""
    spread = kw.pop("spread", SPREAD)
    eff = om.resolve_close_anchor(kw.pop("bid", None), kw.pop("combo_bid", None), spread)
    return om._build_close_order(kw.pop("quantity", 6), kw.pop("limit_price", 0.7500),
                                 kw.pop("market", True), bid=eff,
                                 trigger_type=kw.pop("trigger_type", "stop"))


def test_a_raw_leg_bid_still_cannot_anchor_a_spread(om):
    """Public API contract; production-derived narrative omitted."""
    o = _order(om, bid=0.95, combo_bid=None)
    assert kind(o) == "MKT", (
        "a raw leg bid must NOT produce a bid-anchored limit on a spread")


def test_the_net_combo_bid_does_anchor_and_produces_a_limit(om):
    o = _order(om, combo_bid=0.30)
    assert kind(o) == "LMT", (
        "a synthesised net combo bid should produce a marketable LIMIT, not a MARKET dump")


def test_the_slippage_floor_finally_engages_on_a_spread(om):
    """Public API contract; production-derived narrative omitted."""
    o = _order(om, combo_bid=0.2337, limit_price=0.7500)
    px = float(price(o))
    assert px >= 0.7500 * 0.50 - 1e-9, (
        "priced at %.4f, below the mark*(1-0.50) floor of %.4f -- this is the dump the floor "
        "exists to stop" % (px, 0.7500 * 0.50))


def test_a_healthy_book_is_not_repriced_away_from_the_bid(om):
    """Public API contract; production-derived narrative omitted."""
    o = _order(om, combo_bid=0.60, limit_price=0.7500)
    assert abs(price(o) - 0.60) < 1e-6, (
        "a healthy bid above the floor must be used as-is")


@pytest.mark.parametrize("bad", [0.0, -0.25, float("nan")])
def test_a_broken_net_falls_back_to_guaranteed_fill(om, bad):
    """Public API contract; production-derived narrative omitted."""
    o = _order(om, combo_bid=bad)
    assert kind(o) == "MKT", (
        "combo_bid=%r must fall through to the existing fallback, not anchor" % bad)


def test_a_single_leg_is_unaffected(om):
    """Public API contract; production-derived narrative omitted."""
    o = _order(om, spread=None, bid=0.95, limit_price=1.00)
    assert kind(o) == "LMT"
    assert abs(price(o) - 0.95) < 1e-6










def test_resolver_refuses_a_non_positive_net(om):
    assert om.resolve_close_anchor(None, 0.0, SPREAD) is None
    assert om.resolve_close_anchor(None, -0.25, SPREAD) is None


def test_resolver_refuses_a_nan_net(om):
    assert om.resolve_close_anchor(None, float("nan"), SPREAD) is None


def test_resolver_refuses_an_unparseable_net(om):
    assert om.resolve_close_anchor(None, "wide", SPREAD) is None
    assert om.resolve_close_anchor(None, object(), SPREAD) is None


def test_resolver_returns_the_net_when_it_is_sound(om):
    assert om.resolve_close_anchor(None, 0.30, SPREAD) == pytest.approx(0.30)


def test_resolver_never_lets_a_leg_bid_through_on_a_spread(om):
    """Public API contract; production-derived narrative omitted."""
    assert om.resolve_close_anchor(0.95, None, SPREAD) is None
    assert om.resolve_close_anchor(0.95, 0.30, SPREAD) == pytest.approx(0.30), (
        "the NET may anchor; the LEG bid must never be what anchors")


def test_resolver_passes_a_single_leg_bid_straight_through(om):
    assert om.resolve_close_anchor(0.95, None, None) == pytest.approx(0.95)
