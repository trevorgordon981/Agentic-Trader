import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import flex_reconcile
import exitmgr.connection as connection_mod
from exitmgr.connection import IBConnection
from exitmgr.trader import _market_open_at, entry_window_wait_seconds
from run_trader import _protective_quote_lane_plan


class _Ticker:
    def __init__(self, contract):
        self.contract = contract


class _Contract:
    def __init__(self, con_id):
        self.conId = con_id

    def __hash__(self):
        return int(self.conId)


class _Wrapper:
    def __init__(self):
        self._futures = {}
        self._results = {}
        self._reqId2Contract = {}
        self.reqId2Ticker = {}
        self.ticker2ReqId = defaultdict(dict)
        self.tickers = {}

    def startReq(self, key, contract=None):
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        self._results[key] = []
        self._reqId2Contract[key] = contract
        return future

    def _endReq(self, key):
        future = self._futures.pop(key, None)
        self._reqId2Contract.pop(key, None)
        result = self._results.pop(key, [])
        if future is not None and not future.done():
            future.set_result(result)

    def startTicker(self, req_id, contract, tick_type):
        ticker = self.tickers.get(hash(contract))
        if ticker is None:
            ticker = _Ticker(contract)
            self.tickers[hash(contract)] = ticker
        self.reqId2Ticker[req_id] = ticker
        self._reqId2Contract[req_id] = contract
        self.ticker2ReqId[tick_type][ticker] = req_id
        return ticker

    def endTicker(self, ticker, tick_type):
        req_id = self.ticker2ReqId[tick_type].pop(ticker, 0)
        self._reqId2Contract.pop(req_id, None)
        return req_id


class _Client:
    def __init__(self, wrapper, complete):
        self.wrapper = wrapper
        self.complete = complete
        self.next_id = 700
        self.requested = []
        self.cancelled = []

    def getReqId(self):
        self.next_id += 1
        return self.next_id

    def reqMktData(self, req_id, contract, *_args):
        self.requested.append(req_id)
        if self.complete:
            asyncio.get_running_loop().call_soon(self.wrapper._endReq, req_id)

    def cancelMktData(self, req_id):
        self.cancelled.append(req_id)


def _fake_ib(complete):
    wrapper = _Wrapper()
    client = _Client(wrapper, complete)
    return SimpleNamespace(wrapper=wrapper, client=client), wrapper, client


@pytest.mark.asyncio
async def test_snapshot_success_retires_wrapper_maps_without_cancel():
    conn = IBConnection("127.0.0.1", 4002, 118)
    conn.ib, wrapper, client = _fake_ib(complete=True)
    contracts = [_Contract(1), _Contract(2)]

    tickers = await conn._snapshot_tickers(contracts, 0.1)

    assert [t.contract.conId for t in tickers] == [1, 2]
    assert client.cancelled == []
    assert wrapper._futures == {}
    assert wrapper._results == {}
    assert wrapper.reqId2Ticker == {}
    assert dict(wrapper.ticker2ReqId["snapshot"]) == {}
    assert wrapper.tickers == {}


@pytest.mark.asyncio
async def test_snapshot_timeout_cancels_exact_ids_and_leaves_no_wrapper_maps():
    conn = IBConnection("127.0.0.1", 4002, 118)
    conn.ib, wrapper, client = _fake_ib(complete=False)

    with pytest.raises(asyncio.TimeoutError):
        await conn._snapshot_tickers(
            [_Contract(1), _Contract(2)], 0.01)

    assert client.cancelled == client.requested == [701, 702]
    assert wrapper._futures == {}
    assert wrapper._results == {}
    assert wrapper.reqId2Ticker == {}
    assert dict(wrapper.ticker2ReqId["snapshot"]) == {}
    assert wrapper.tickers == {}


@pytest.mark.asyncio
async def test_each_snapshot_uses_a_fresh_ticker_not_prior_cycle_prices():
    conn = IBConnection("127.0.0.1", 4002, 118)
    conn.ib, wrapper, _client = _fake_ib(complete=True)
    contract = _Contract(1)

    first = (await conn._snapshot_tickers([contract], 0.1))[0]
    first.bid = 9.99
    second = (await conn._snapshot_tickers([contract], 0.1))[0]

    assert second is not first
    assert not hasattr(second, "bid")
    assert wrapper.tickers == {}


@pytest.mark.asyncio
async def test_snapshot_lifecycle_refuses_unreviewed_ib_async_version(monkeypatch):
    conn = IBConnection("127.0.0.1", 4002, 118)
    conn.ib, _wrapper, client = _fake_ib(complete=True)
    monkeypatch.setattr(connection_mod.importlib_metadata, "version", lambda _name: "2.2.0")

    with pytest.raises(RuntimeError, match="unsupported ib_async snapshot lifecycle"):
        await conn._snapshot_tickers([_Contract(1)], 0.1)
    assert client.requested == []


@pytest.mark.asyncio
async def test_snapshot_lifecycle_refuses_changed_private_map_shape(monkeypatch):
    conn = IBConnection("127.0.0.1", 4002, 118)
    conn.ib, wrapper, client = _fake_ib(complete=True)
    wrapper.reqId2Ticker = None
    monkeypatch.setattr(connection_mod.importlib_metadata, "version", lambda _name: "2.1.0")

    with pytest.raises(RuntimeError, match="wrapper.reqId2Ticker"):
        await conn._snapshot_tickers([_Contract(1)], 0.1)
    assert client.requested == []


def test_exchange_clock_handles_summer_winter_and_close_exclusively():
    assert _market_open_at(datetime(2037, 7, 6, 13, 30, tzinfo=timezone.utc))
    assert not _market_open_at(datetime(2037, 1, 5, 13, 30, tzinfo=timezone.utc))
    assert _market_open_at(datetime(2037, 1, 5, 14, 30, tzinfo=timezone.utc))
    assert not _market_open_at(datetime(2037, 1, 5, 21, 0, tzinfo=timezone.utc))


def test_quote_lane_skips_and_backs_off_outside_rth():
    assert _protective_quote_lane_plan(
        datetime(2037, 7, 6, 14, 0, tzinfo=timezone.utc)) == (True, 10.0)
    assert _protective_quote_lane_plan(
        datetime(2037, 7, 6, 21, 0, tzinfo=timezone.utc)) == (False, 300.0)
    assert entry_window_wait_seconds(
        datetime(2037, 1, 5, 13, 30, tzinfo=timezone.utc), delay_min=5) == 3900.0


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_missing_second_campaign_is_append_not_existing_row_rewrite(tmp_path):
    primary, short = 7001001, 7001002
    trades = tmp_path / "trades.log"
    exits = tmp_path / "exits.log"
    state = tmp_path / "state.json"
    statement = tmp_path / "flex-statement-current.xml"
    _write_jsonl(trades, [{
        "ts": "2037-08-18T15:23:52+00:00", "fill_ts": "2037-08-18T15:23:52+00:00",
        "contract_id": primary, "symbol": "SYML", "quantity": 1,
        "entry_fill_debit": 400.0, "basis_source": "fill", "right": "P",
        "order_ref": "entry-SYML",
        "strike": 120.0, "decision_id": "decision-2", "conviction": 7,
        "spread": {"short_con_id": short, "short_strike": 115.0, "width": 5.0},
    }])
    _write_jsonl(exits, [{
        "contract_id": 7001003, "symbol": "SYML", "quantity": 1,
        "close_ts": "2037-08-18T13:41:43+00:00", "realized_pnl": -60.0,
    }])
    inflight = {
        "order_id": 51, "perm_id": 9100001, "client_id": 701,
        "order_ref": "exitmgr-SYML", "placed_at": "2037-08-25T13:29:00+00:00",
        "exit_context": {
            "reason": "stop", "trigger_type": "trailing_stop",
            "extra": {"rule_fired": "trailing_stop"}},
        "submitted_close": {"sec_type": "BAG", "action": "SELL", "combo_qty": 1,
                            "legs": [
            {"con_id": primary, "ratio": 1, "expected_side": "SLD", "multiplier": 100},
            {"con_id": short, "ratio": 1, "expected_side": "BOT", "multiplier": 100}]},
    }
    state.write_text(json.dumps({"in_flight": {str(primary): inflight}}))
    statement.write_text("""<FlexQueryResponse><FlexStatements><FlexStatement
      fromDate="20310818" toDate="20370825"><Trades>
      <Trade underlyingSymbol="SYML" conid="7001001" dateTime="20370818;112351"
        buySell="BUY" openCloseIndicator="O" quantity="1" tradePrice="10.00"
        fifoPnlRealized="0" accountId="acct" orderReference="entry-SYML"
        ibOrderID="open-1" ibExecID="open-long"/>
      <Trade underlyingSymbol="SYML" conid="7001002" dateTime="20370818;112351"
        buySell="SELL" openCloseIndicator="O" quantity="-1" tradePrice="6.00"
        fifoPnlRealized="0" accountId="acct" orderReference="entry-SYML"
        ibOrderID="open-2" ibExecID="open-short"/>
      <Trade underlyingSymbol="SYML" conid="7001001" dateTime="20370825;093021"
        buySell="SELL" openCloseIndicator="C" quantity="-1" tradePrice="12.00"
        fifoPnlRealized="200.0" accountId="acct" orderReference="exitmgr-SYML"
        ibOrderID="close-1" ibExecID="close-long"/>
      <Trade underlyingSymbol="SYML" conid="7001002" dateTime="20370825;093021"
        buySell="BUY" openCloseIndicator="C" quantity="1" tradePrice="7.50"
        fifoPnlRealized="-150.0" accountId="acct" orderReference="exitmgr-SYML"
        ibOrderID="close-2" ibExecID="close-short"/>
      </Trades></FlexStatement></FlexStatements></FlexQueryResponse>""")

    rows = [("SYML", -10.0, -60.0, 50.0, "netted")]
    doc = flex_reconcile.propose_repairs(
        rows, [], {"SYML": -17.455798}, str(statement),
        exits_path=str(exits), trades_path=str(trades), state_path=str(state))

    assert doc["proposals"] == []
    assert len(doc["append_proposals"]) == 1
    proposed = doc["append_proposals"][0]
    assert proposed["con_id"] == primary
    assert proposed["row"]["quantity"] == proposed["row"]["close_qty"] == 1
    assert proposed["row"]["realized_pnl"] == 50.0
    assert proposed["row"]["exit_price_per_share"] == 4.5
    assert proposed["row"]["broker_open_order_ref"] == "entry-SYML"
    assert proposed["row"]["broker_close_order_ref"] == "exitmgr-SYML"
    assert proposed["row"]["broker_open_ib_order_ids"] == ["open-1", "open-2"]
    assert proposed["row"]["broker_close_ib_order_ids"] == ["close-1", "close-2"]
    assert len(doc["state_retire_proposals"]) == 1
    assert "append that campaign" in doc["refused"][0]["why"]

    exact_xml = statement.read_text()
    statement.write_text(exact_xml.replace(
        'orderReference="exitmgr-SYML"', 'orderReference="wrong-close"'))
    mismatch = flex_reconcile.propose_missing_campaign_closes(
        str(statement), exits_path=str(exits), trades_path=str(trades), state_path=str(state))
    assert mismatch["append_proposals"] == []
    assert mismatch["state_retire_proposals"] == []
    assert "closing orderReference" in mismatch["refused"][0]["why"]

    statement.write_text(exact_xml.replace(
        'ibExecID="close-short"', 'ibExecID="close-long"'))
    duplicate = flex_reconcile.propose_missing_campaign_closes(
        str(statement), exits_path=str(exits), trades_path=str(trades), state_path=str(state))
    assert duplicate["append_proposals"] == []
    assert "execution IDs are absent or duplicated" in duplicate["refused"][0]["why"]

    statement.write_text(exact_xml.replace(
        'dateTime="20370818;112351"', 'dateTime="20370818;112401"', 1))
    timestamp_drift = flex_reconcile.propose_missing_campaign_closes(
        str(statement), exits_path=str(exits), trades_path=str(trades), state_path=str(state))
    assert timestamp_drift["append_proposals"] == []
    assert "opening legs do not share and match" in timestamp_drift["refused"][0]["why"]

    statement.write_text(exact_xml)
    loose_state = json.loads(state.read_text())
    loose_state["in_flight"][str(primary)]["submitted_close"]["legs"][0]["ratio"] = 2
    state.write_text(json.dumps(loose_state))
    wrong_bag = flex_reconcile.propose_missing_campaign_closes(
        str(statement), exits_path=str(exits), trades_path=str(trades), state_path=str(state))
    assert wrong_bag["append_proposals"] == []
    assert "submitted-close BAG" in wrong_bag["refused"][0]["why"]

    state.write_text(json.dumps({"in_flight": {str(primary): inflight}}))
    nan_entry = json.loads(trades.read_text().strip())
    nan_entry["entry_fill_debit"] = float("nan")
    _write_jsonl(trades, [nan_entry])
    nonfinite = flex_reconcile.propose_missing_campaign_closes(
        str(statement), exits_path=str(exits), trades_path=str(trades), state_path=str(state))
    assert nonfinite["append_proposals"] == []
    assert "not finite" in nonfinite["refused"][0]["why"]


def test_missing_campaign_refuses_when_opening_debit_does_not_bind(tmp_path):
    primary, short = 1, 2
    trades, exits = tmp_path / "trades", tmp_path / "exits"
    state, statement = tmp_path / "state", tmp_path / "statement.xml"
    _write_jsonl(trades, [{"ts": "2037-01-01T15:00:00+00:00", "contract_id": primary,
                          "symbol": "X", "quantity": 1, "entry_fill_debit": 999,
                          "order_ref": "entry-x",
                          "spread": {"short_con_id": short}}])
    exits.write_text("")
    state.write_text(json.dumps({"in_flight": {"1": {
        "order_ref": "exit-x", "placed_at": "2037-01-02T14:59:00+00:00",
        "submitted_close": {"sec_type": "BAG", "action": "SELL", "combo_qty": 1,
                            "legs": [
                                {"con_id": 1, "ratio": 1, "expected_side": "SLD",
                                 "multiplier": 100},
                                {"con_id": 2, "ratio": 1, "expected_side": "BOT",
                                 "multiplier": 100}]}}}}))
    statement.write_text("""<R><Trade conid="1" dateTime="20370101;100000" buySell="BUY"
      openCloseIndicator="O" quantity="1" tradePrice="5" fifoPnlRealized="0"
      accountId="acct" orderReference="entry-x" ibOrderID="open-1" ibExecID="open-1"/>
      <Trade conid="2" dateTime="20370101;100000" buySell="SELL" openCloseIndicator="O"
      quantity="-1" tradePrice="4" fifoPnlRealized="0"
      accountId="acct" orderReference="entry-x" ibOrderID="open-2" ibExecID="open-2"/>
      <Trade conid="1" dateTime="20370102;100000" buySell="SELL" openCloseIndicator="C"
      quantity="-1" tradePrice="6" fifoPnlRealized="100" accountId="acct"
      orderReference="exit-x" ibOrderID="close-1" ibExecID="a"/>
      <Trade conid="2" dateTime="20370102;100000" buySell="BUY" openCloseIndicator="C"
      quantity="1" tradePrice="4" fifoPnlRealized="0" accountId="acct"
      orderReference="exit-x" ibOrderID="close-2" ibExecID="b"/></R>""")

    doc = flex_reconcile.propose_missing_campaign_closes(
        str(statement), exits_path=str(exits), trades_path=str(trades), state_path=str(state))
    assert doc["append_proposals"] == []
    assert "opening legs produce debit" in doc["refused"][0]["why"]
