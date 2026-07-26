from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import daily_recommend as dr
from exitmgr import apewisdom as aw


def good_row(ticker="MU"):
    values = [round(i * 100 / 30) for i in range(31)]
    return {"ticker": ticker, "rank": 1, "name": ticker,
            "mention_trend_30d": values,
            "mention_trend_30d_sparkline": aw.trend_sparkline(values)}


class FakeIB:
    async def qualifyContractsAsync(self, _stock):
        return [SimpleNamespace(conId=123, secType="STK", currency="USD")]

    async def reqSecDefOptParamsAsync(self, *_args):
        return [SimpleNamespace(exchange="SMART", tradingClass="MU")]


class IncompleteContractIB(FakeIB):
    async def qualifyContractsAsync(self, _stock):
        return [SimpleNamespace(conId=123)]


@pytest.mark.asyncio
async def test_probe_requires_profile_stock_and_options_chain(monkeypatch):
    monkeypatch.setattr(aw, "security_profile", lambda _ticker: {
        "quote_type": "EQUITY", "currency": "USD", "price": 50.0,
        "average_volume": 5_000_000, "industry": "Semiconductors", "sector": "Technology"})
    monkeypatch.setattr(dr, "pick_chain", lambda params, _ticker: params[0])
    row, reason = await dr._probe_apewisdom_row(FakeIB(), good_row(), ["biotech"],
                                                 __import__("asyncio").Semaphore(1))
    assert row["ticker"] == "MU" and reason == "eligible"


@pytest.mark.asyncio
async def test_probe_rejects_contract_missing_explicit_stock_currency_identity(monkeypatch):
    monkeypatch.setattr(aw, "security_profile", lambda _ticker: {
        "quote_type": "EQUITY", "currency": "USD", "price": 50.0,
        "average_volume": 5_000_000, "industry": "Semiconductors", "sector": "Technology"})
    row, reason = await dr._probe_apewisdom_row(
        IncompleteContractIB(), good_row(), ["biotech"], __import__("asyncio").Semaphore(1))
    assert row is None and reason == "smart_usd_stock_unqualified"


@pytest.mark.asyncio
async def test_source_failure_is_fail_soft_and_audited(monkeypatch):
    events = []
    monkeypatch.setattr(aw, "load_trends",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(aw.ApeWisdomError("down")))
    monkeypatch.setattr(dr, "audit", lambda _path, event, **kw: events.append((event, kw)))
    feed, rows = await dr._load_apewisdom_pool(
        FakeIB(), {"apewisdom_discovery": {"enabled": True}}, "/tmp/no-write")
    assert feed is None and rows == []
    assert events[0][0] == "apewisdom_source_error"


def test_discovery_merge_dedupes_without_mutation():
    broad = [("AAPL", "broad"), ("MU", "broad duplicate")]
    ape = [("MU", "attention"), ("IBM", "attention")]
    assert dr._merge_discovery_candidates(broad, ape) == [
        ("AAPL", "broad"), ("MU", "broad duplicate"), ("IBM", "attention")]


def test_main_proposal_and_training_capture_never_receive_attention_context():
    source = inspect.getsource(dr.run)
    assert "_ape_discovery_brief = apewisdom.discovery_context" in source
    assert "_res = propose(tr.get(\"llm_endpoint\"), tr.get(\"llm_model\"), brief," in source
    assert "market_context=brief" in source
    assert "market_context=_ape_discovery_brief" not in source
    assert "technical_card=_ape_discovery_brief" not in source
    assert "propose(tr.get(\"llm_endpoint\"), tr.get(\"llm_model\"), _ape_discovery_brief" not in source
    assert "research.gather(ib, names" in source
    assert "propose_one(tr.get(\"llm_endpoint\"), tr.get(\"llm_model\"), _one_brief" in source
    assert "market_context=capture_context" in source
    assert "raw_strategist=capture_raw, cot=capture_cot" in source
    assert "technical_card=capture_technical_card" in source
    assert "price_stats=_slate_price_stats" in source


def test_clarified_source_fields_exclude_24h_and_upvotes():
    context = aw.discovery_context(
        "plain market brief", [good_row()], watched=(),
        price_stats={"MU": {"ret_20d": 4.56}})
    bound = aw.bind_reviewed_candidates(
        [("MU", "ignore me")], [good_row()],
        price_stats={"MU": {"ret_20d": 4.56}})
    combined = context + "\n" + bound[0][1]
    assert "rank #1" in combined
    assert "30d mention" in combined
    assert "20 sessions) +4.6%" in combined
    for forbidden in ("mentions_24h", "mention_delta", "rank_delta", "upvotes", "24h"):
        assert forbidden not in combined


def test_existing_human_add_and_explicit_approval_gates_remain_present():
    source = inspect.getsource(dr.run)
    assert "approval.parse_add_tickers" in source
    assert "_append_watchlist(args.config, new)" in source
    assert "decision_from_reactions(reactions, approver_ids) == \"approve\"" in source
    assert "decision_from_replies(replies, approver_ids, ts) == \"approve\"" in source
