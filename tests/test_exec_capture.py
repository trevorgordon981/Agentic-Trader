"""Unit tests for exitmgr.exec_capture -- MANUAL/external IBKR fill capture (2026-07-03).

All mocked: no IB connection. Covers manual-tagging, app-origin dedup, entry<->exit pairing,
close-only (opener pre-window), open-position snapshots, commission honesty, no-fabrication of
reasoning, and watermark idempotency."""
import json
import types

import pytest

from exitmgr import exec_capture as ec


# --------------------------------------------------------------------------- fake IB objects
class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_fill(*, exec_id, con_id=555, symbol="AAPL", right="C", strike=200.0,
               expiry="20260717", side="BOT", shares=1, price=5.0, client_id=0,
               order_id=0, perm_id=0, commission=1.05, realized=None, sec_type="OPT"):
    """Build an object shaped like an ib_async Fill (contract/execution/commissionReport)."""
    contract = _Obj(conId=con_id, symbol=symbol, right=right, strike=strike,
                    lastTradeDateOrContractMonth=expiry, secType=sec_type, multiplier="100")
    execution = _Obj(execId=exec_id, orderId=order_id, permId=perm_id, clientId=client_id,
                     acctNumber="U123", side=side, shares=shares, price=price,
                     time="2026-07-02T14:30:00")
    cr = _Obj(execId=exec_id, commission=commission, currency="USD",
              realizedPNL=(realized if realized is not None else ec._UNSET_DOUBLE))
    return _Obj(contract=contract, execution=execution, commissionReport=cr)


def _norm(**kw):
    return ec.normalize_fill(_fake_fill(**kw))


EMPTY_IDX = {"exec_ids": set(), "order_ids": set(), "perm_ids": set(), "trade_uids": set()}
APP_CIDS = {42, 88, 91}


# --------------------------------------------------------------------------- normalize
def test_normalize_filters_unset_realized_pnl():
    # opening fill: IBKR writes UNSET_DOUBLE -> normalized to None (treated as an OPEN)
    f = _norm(exec_id="e1", side="BOT")
    assert f["realized_pnl_ib"] is None
    assert f["con_id"] == 555 and f["symbol"] == "AAPL" and f["mult"] == 100.0
    # closing fill: real realizedPNL survives
    g = _norm(exec_id="e2", side="SLD", realized=42.5)
    assert g["realized_pnl_ib"] == 42.5


def test_normalize_none_execid_returns_none():
    assert ec.normalize_fill(_Obj(execution=None, contract=None, commissionReport=None)) is None


# --------------------------------------------------------------------------- classification
def test_manual_when_clientid_zero():
    f = _norm(exec_id="e1", client_id=0)
    app, reason = ec.is_app_origin(f, EMPTY_IDX, APP_CIDS)
    assert app is False and reason == "manual_tws_clientid0"


def test_nonzero_clientid_is_app_origin():
    # Alfred's API order (even a rotated id not in the allowlist) is NEVER tagged manual
    f = _norm(exec_id="e1", client_id=7777)
    app, _ = ec.is_app_origin(f, EMPTY_IDX, APP_CIDS)
    assert app is True
    f2 = _norm(exec_id="e2", client_id=88)
    assert ec.is_app_origin(f2, EMPTY_IDX, APP_CIDS)[0] is True


def test_app_origin_by_dataset_id_match():
    f = _norm(exec_id="ex-9", client_id=0, order_id=1234)
    idx = {**EMPTY_IDX, "order_ids": {1234}}
    assert ec.is_app_origin(f, idx, APP_CIDS)[0] is True  # order_id already in dataset -> skip
    idx2 = {**EMPTY_IDX, "exec_ids": {"ex-9"}}
    assert ec.is_app_origin(f, idx2, APP_CIDS)[0] is True  # exec_id already captured -> skip


# --------------------------------------------------------------------------- pairing: round trip
def test_full_round_trip_pairs_into_one_trade_with_real_pnl():
    opens = [_norm(exec_id="o1", side="BOT", shares=2, price=5.00, commission=1.0)]
    closes = [_norm(exec_id="c1", side="SLD", shares=2, price=7.00, commission=1.0, realized=398.0)]
    built = ec.build_rows_for_contract(555, opens + closes)
    tr = built["trade"]
    assert tr is not None and built["position"] is None and built["terminal"] is True
    assert tr["kind"] == "trade" and tr["source"] == "manual"
    # debit = 5.00 * 2 * 100 = 1000; proceeds = 7.00 * 2 * 100 = 1400; gross = 400
    assert tr["entry"]["debit"] == 1000.0
    assert tr["close"]["proceeds"] == 1400.0
    assert tr["close"]["realized_pnl"] == 400.0
    # net = gross - (1.0 + 1.0) commissions = 398.0
    assert tr["close"]["realized_pnl_net"] == 398.0
    assert tr["close"]["commission_unknown"] is False
    assert tr["close"]["realized_pnl_pct"] == 40.0
    assert tr["labels"]["outcome"] == "win" and tr["labels"]["win"] is True


def test_missing_commission_marks_unknown_and_null_net():
    opens = [_norm(exec_id="o1", side="BOT", shares=1, price=4.0, commission=None)]
    closes = [_norm(exec_id="c1", side="SLD", shares=1, price=3.0, commission=1.0, realized=-101.0)]
    tr = ec.build_rows_for_contract(555, opens + closes)["trade"]
    assert tr["close"]["commission_unknown"] is True
    assert tr["close"]["realized_pnl_net"] is None
    assert tr["close"]["realized_pnl"] == -100.0     # gross still real
    assert tr["labels"]["outcome"] == "loss"


# --------------------------------------------------------------------------- pairing: close-only
def test_close_only_opener_pre_window_uses_ib_realized_and_null_basis():
    # only a closing SLD fill visible (opener predates the ~7d window)
    closes = [_norm(exec_id="c1", side="SLD", shares=1, price=6.0, commission=1.0, realized=250.0)]
    built = ec.build_rows_for_contract(555, closes)
    tr = built["trade"]
    assert tr is not None
    assert tr["entry"]["debit"] is None                      # NEVER fabricated
    assert tr["entry"]["entry_outside_window"] is True
    assert tr["close"]["realized_pnl"] == 250.0              # IBKR's REAL realizedPNL
    assert "Flex Query" in tr["close"]["basis_source"]
    assert tr["close"]["realized_pnl_pct"] is None
    assert built["terminal"] is True


# --------------------------------------------------------------------------- pairing: open only
def test_open_only_emits_position_snapshot_not_terminal():
    opens = [_norm(exec_id="o1", side="BOT", shares=3, price=2.5, commission=1.0)]
    built = ec.build_rows_for_contract(555, opens)
    assert built["trade"] is None
    pos = built["position"]
    assert pos is not None and pos["kind"] == "position" and pos["status"] == "open"
    assert pos["position"]["quantity"] == 3
    assert pos["position"]["open_cost"] == 750.0             # 2.5 * 3 * 100
    assert built["terminal"] is False


# --------------------------------------------------------------------------- no fabrication
def test_manual_trade_has_null_reasoning_never_fabricated():
    opens = [_norm(exec_id="o1", side="BOT", price=5.0, realized=None)]
    closes = [_norm(exec_id="c1", side="SLD", price=6.0, realized=100.0)]
    tr = ec.build_rows_for_contract(555, opens + closes)["trade"]
    assert tr["decision"] is None
    assert tr["entry"]["thesis"] is None
    assert tr["entry"]["conviction"] is None
    assert tr["reasoning_available"] is False
    assert tr["manual"] is True
    assert tr["lifecycle"]["mark_path"] == [] and tr["lifecycle"]["mfe_pct"] is None


# --------------------------------------------------------------------------- process + dedup + watermark
def test_process_filters_app_origin_and_pairs_manual():
    fills = [
        _norm(exec_id="o1", side="BOT", price=5.0, client_id=0),                 # manual open
        _norm(exec_id="c1", side="SLD", price=7.0, client_id=0, realized=398.0), # manual close
        _norm(exec_id="a1", side="BOT", price=1.0, client_id=88, con_id=999),    # Alfred -> skip
    ]
    wm = {"_processed": set()}
    res = ec.process_fills(fills, EMPTY_IDX, wm, APP_CIDS)
    assert res["stats"]["app_origin"] == 1
    assert res["stats"]["manual"] == 2
    assert len(res["trade_rows"]) == 1
    assert res["new_processed"] == {"o1", "c1"}


def test_watermark_idempotency_no_reemit():
    fills = [
        _norm(exec_id="o1", side="BOT", price=5.0, client_id=0),
        _norm(exec_id="c1", side="SLD", price=7.0, client_id=0, realized=398.0),
    ]
    wm = {"_processed": {"o1", "c1"}}          # already folded into a terminal trade
    res = ec.process_fills(fills, EMPTY_IDX, wm, APP_CIDS)
    assert res["trade_rows"] == []             # nothing re-emitted
    assert res["stats"]["already_watermarked"] == 1


def test_watermark_roundtrip(tmp_path):
    ddir = str(tmp_path)
    wm = ec.load_watermark(ddir)
    wm["_processed"].update({"x1", "x2"})
    wm["runs"] = 1
    ec.save_watermark(ddir, wm)
    wm2 = ec.load_watermark(ddir)
    assert wm2["_processed"] == {"x1", "x2"}
    assert wm2["runs"] == 1


def test_append_rows_dedup_and_idempotent(tmp_path):
    dpath = str(tmp_path / "trade_dataset.jsonl")
    opens = [_norm(exec_id="o1", side="BOT", price=5.0)]
    closes = [_norm(exec_id="c1", side="SLD", price=7.0, realized=398.0)]
    tr = ec.build_rows_for_contract(555, opens + closes)["trade"]
    n1 = ec._append_rows(dpath, [tr], dry_run=False)
    n2 = ec._append_rows(dpath, [tr], dry_run=False)   # same _dedup_key -> no re-append
    assert n1 == 1 and n2 == 0
    with open(dpath) as f:
        assert sum(1 for _ in f) == 1


def test_load_app_origin_index_harvests_ids(tmp_path):
    dpath = str(tmp_path / "trade_dataset.jsonl")
    row = {"kind": "trade", "con_id": 1, "close": {"order_id": 4242},
           "provenance": {"exec_ids": ["EX-1"], "perm_ids": [9]}}
    with open(dpath, "w") as f:
        f.write(json.dumps(row) + "\n")
    idx = ec.load_app_origin_index(dpath)
    assert 4242 in idx["order_ids"]
    assert "EX-1" in idx["exec_ids"]
    assert 9 in idx["perm_ids"]
