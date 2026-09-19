from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from exitmgr.connection import _record_snapshot_quote_times, _positive_finite_quote_value, OrderData, OrderView


def test_actual_bid_ask_receipts_ignore_greek_and_last_updates():
    t=datetime(2026,9,18,19,tzinfo=timezone.utc)
    ticker=SimpleNamespace(ticks=[SimpleNamespace(tickType=1,time=t), SimpleNamespace(tickType=2,time=t+timedelta(seconds=1)), SimpleNamespace(tickType=4,time=t+timedelta(seconds=20))])
    _record_snapshot_quote_times(ticker)
    assert ticker._exitmgr_bid_observed_utc==t.isoformat()
    assert ticker._exitmgr_ask_observed_utc==(t+timedelta(seconds=1)).isoformat()
    ticker.ticks=[SimpleNamespace(tickType=4,time=t+timedelta(seconds=30))]
    _record_snapshot_quote_times(ticker)
    assert ticker._exitmgr_bid_observed_utc==t.isoformat()


def test_missing_or_nonfinite_tick_is_unknown_not_default_cent():
    for v in (None,0,-1,float('nan'),float('inf')):
        assert _positive_finite_quote_value(v) is None
    assert _positive_finite_quote_value(.05)==.05


def test_broker_order_price_survives_state_projection_for_recovery_ack():
    row=OrderData(23,99,2,limit_price=1.25,perm_id=333,client_id=189,order_ref='exact')
    assert OrderView.known({23:row}).as_state_dicts()[23]['limit_price']==1.25
