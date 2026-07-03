"""Gap-fix (2026-07-03): prove the config-driven exit_slippage_floor actually FLOORS a triggered
bid-anchored SELL-to-close. The wiring (config.rules.exit_slippage_floor -> ExitManager ->
OrderManager) is covered by test_wire_config_seams (Seam 1); this test proves the END behaviour:
a would-be below-floor exit price is clamped UP to mark*(1 - floor)."""
from unittest.mock import MagicMock

from exitmgr.order import OrderManager


def _om(floor):
    ib = MagicMock()
    # create_limit_order records the price it was handed so we can assert on it.
    ib.create_limit_order = MagicMock(return_value="LMT_ORDER")
    ib.create_market_order = MagicMock(return_value="MKT_ORDER")
    return OrderManager(ib, MagicMock(), exit_slippage_floor=floor), ib


def test_below_floor_bid_is_clamped_up_to_the_floor():
    om, ib = _om(0.30)               # floor 30% -> min price = mark*(1-0.30)
    # mark 1.00, junk/stub bid 0.10 (would dump for pennies). Floor = 1.00*0.70 = 0.70.
    om._build_close_order(quantity=4, limit_price=1.00, market=True,
                          bid=0.10, trigger_type="stop_loss")
    _action, _qty, px = ib.create_limit_order.call_args.args
    assert _action == "SELL" and _qty == 4
    assert px == 0.70, f"below-floor bid not clamped: got {px}, expected floor 0.70"


def test_bid_above_floor_uses_the_bid():
    om, ib = _om(0.30)
    # mark 1.00, healthy bid 0.90 (above the 0.70 floor) -> cross at the bid, not the floor.
    om._build_close_order(quantity=2, limit_price=1.00, market=True,
                          bid=0.90, trigger_type="stop_loss")
    _action, _qty, px = ib.create_limit_order.call_args.args
    assert px == 0.90


def test_custom_config_floor_changes_the_clamp():
    # A tuned config floor (0.20) -> floor 0.80; the same junk bid clamps to 0.80, not 0.70.
    om, ib = _om(0.20)
    om._build_close_order(quantity=1, limit_price=1.00, market=True,
                          bid=0.05, trigger_type="stop_loss")
    _action, _qty, px = ib.create_limit_order.call_args.args
    assert px == 0.80
