"""Tests for the earnings-blackout gate WIRED INTO the LIVE trader loop (trader.py).

The gate itself (construction.earnings_ok) is unit-tested in test_earnings_gate.py. THIS file
proves trader.py's own construction path (Trader.run_once, the intraday entry loop — NOT
daily_recommend) actually calls it and reacts correctly:

  * a DEBIT whose known earnings falls on/before expiry  -> BLOCKED: never submitted, a
    capture_rejected/audit "earnings_blackout_rejected" row is written, a Slack skip posted
  * earnings AFTER expiry                                -> PASSES the gate, reaches submit
  * UNKNOWN earnings (days_to_earnings None)             -> fail-open: reaches submit BUT the
    'earnings_unchecked' state is surfaced (Slack note + resolved flag), never silent-cleared

Everything is mocked: research.days_to_earnings (no yfinance/network), the broker/IBKR
(_resolve_order + _submit_order), approval, market clock, and the pot snapshot.
"""
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock

import exitmgr.trader as trader
from exitmgr.trader import Trader, ResolvedOrder
from exitmgr.risk import RiskLimits
from exitmgr.strategist import TradeIdea
from exitmgr.account import PotSnapshot
from exitmgr.config import ConstructionConfig

LIM = RiskLimits()
IDEA = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")


def _expiry_str(days_from_today: int) -> str:
    d = datetime.now(timezone.utc).date() + timedelta(days=days_from_today)
    return d.strftime("%Y%m%d")


def _trader(tmp_path, resolved):
    ibc = MagicMock()
    ibc.ib = MagicMock()
    ibc.get_positions = AsyncMock(return_value={})
    em = MagicMock(); em.run_cycle = AsyncMock()
    t = Trader(ib_conn=ibc, exit_manager=em, limits=LIM, approved_names=set(),
               endpoint="http://x", model="m", slack_token="tok", slack_channel="C1",
               approver_ids={"OWNER"}, baseline_path=str(tmp_path / "b.json"),
               audit_path=str(tmp_path / "a.jsonl"), approve_timeout_s=60,
               construction_cfg=ConstructionConfig(earnings_blackout_enabled=True))
    t._resolve_order = AsyncMock(return_value=resolved)
    t._submit_order = AsyncMock(return_value=("Filled", []))
    return t


def _wire(monkeypatch, posts, days_to_earnings):
    monkeypatch.setattr(trader.research, "gather", AsyncMock(return_value={}))  # no network
    monkeypatch.setattr(trader, "_market_open", lambda: True)                   # deterministic RTH
    monkeypatch.setattr(trader, "get_pot_snapshot",
                        AsyncMock(return_value=PotSnapshot(1010.0, 9000.0, 1010.0)))
    monkeypatch.setattr(trader, "propose", lambda *a, **k: [IDEA])
    monkeypatch.setattr(trader.approval, "post_proposal",
                        lambda tok, ch, txt: posts.append(txt) or "ts1")
    monkeypatch.setattr(trader.approval, "await_approval", lambda *a, **k: "approve")
    # THE gate's data source — mocked so no yfinance call happens
    monkeypatch.setattr(trader.research, "days_to_earnings", lambda *a, **k: days_to_earnings)


@pytest.mark.asyncio
async def test_trader_blocks_debit_with_earnings_before_expiry(tmp_path, monkeypatch):
    # expiry ~120d out; earnings in 30d => earnings BEFORE expiry => must BLOCK
    resolved = ResolvedOrder("SPY", "C", _expiry_str(120), 50.0, 1, 1.20, object(), dte=120)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=30)
    t = _trader(tmp_path, resolved)
    await t.run_once(dry_run=False)
    # INVARIANT: an earnings-straddling debit is never submitted
    t._submit_order.assert_not_called()
    # the skip is surfaced to Slack and audited
    assert any("Skipped" in p and "earnings" in p.lower() for p in posts), posts
    audit_rows = (tmp_path / "a.jsonl").read_text()
    assert "earnings_blackout_rejected" in audit_rows


@pytest.mark.asyncio
async def test_trader_passes_debit_with_earnings_after_expiry(tmp_path, monkeypatch):
    # expiry ~20d out; earnings in 90d => earnings AFTER expiry => gate PASSES => submits
    resolved = ResolvedOrder("SPY", "C", _expiry_str(20), 50.0, 1, 1.20, object(), dte=20)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=90)
    t = _trader(tmp_path, resolved)
    await t.run_once(dry_run=False)
    t._submit_order.assert_awaited_once()
    # not blocked, not flagged unchecked
    assert resolved.earnings_unchecked is False
    assert not any("UNCHECKED" in p for p in posts), posts
    assert "earnings_blackout_rejected" not in (tmp_path / "a.jsonl").read_text()


@pytest.mark.asyncio
async def test_trader_fails_open_and_flags_when_earnings_unknown(tmp_path, monkeypatch):
    # days_to_earnings None => unknown => FAIL-OPEN (submits) but earnings_unchecked surfaced
    resolved = ResolvedOrder("SPY", "C", _expiry_str(120), 50.0, 1, 1.20, object(), dte=120)
    posts = []
    _wire(monkeypatch, posts, days_to_earnings=None)
    t = _trader(tmp_path, resolved)
    await t.run_once(dry_run=False)
    t._submit_order.assert_awaited_once()          # fail-open: not hard-blocked
    assert resolved.earnings_unchecked is True     # but never silent-cleared
    assert any("UNCHECKED" in p for p in posts), posts   # surfaced in the Slack proposal
    assert "earnings_unchecked" in (tmp_path / "a.jsonl").read_text()  # and audited
