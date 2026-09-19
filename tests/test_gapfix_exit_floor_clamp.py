"""Public API contract; production-derived narrative omitted."""
from unittest.mock import MagicMock

from exitmgr.order import OrderManager


def _om(floor):
    ib = MagicMock()

    ib.create_limit_order = MagicMock(return_value="LMT_ORDER")
    ib.create_market_order = MagicMock(return_value="MKT_ORDER")
    return OrderManager(ib, MagicMock(), exit_slippage_floor=floor), ib


def test_below_floor_bid_is_clamped_up_to_the_floor():
    om, ib = _om(0.30)

    om._build_close_order(quantity=4, limit_price=1.00, market=True,
                          bid=0.10, trigger_type="stop_loss")
    _action, _qty, px = ib.create_limit_order.call_args.args
    assert _action == "SELL" and _qty == 4
    assert px == 0.70, f"below-floor bid not clamped: got {px}, expected floor 0.70"


def test_bid_above_floor_uses_the_bid():
    om, ib = _om(0.30)

    om._build_close_order(quantity=2, limit_price=1.00, market=True,
                          bid=0.90, trigger_type="stop_loss")
    _action, _qty, px = ib.create_limit_order.call_args.args
    assert px == 0.90


def test_custom_config_floor_changes_the_clamp():

    om, ib = _om(0.20)
    om._build_close_order(quantity=1, limit_price=1.00, market=True,
                          bid=0.05, trigger_type="stop_loss")
    _action, _qty, px = ib.create_limit_order.call_args.args
    assert px == 0.80
