# exitmgr — LLM-assisted options trading (propose → gate → approve → execute → manage)

A small system where the model **proposes** option swing trades and you **approve every entry**,
while hard-coded risk rails and an exit manager keep the book bounded. Built for a small,
ring-fenced IBKR pot.

> **Money warning.** An LLM has no proven trading edge. Treat the pot as risk capital you are
> prepared to lose. Run on **paper for weeks** and watch the proposals before risking a cent.

## Safety model (read this)
- **Dry-run is the default.** No order is ever placed without `--arm`.
- **Even with `--arm`, every entry needs an explicit Slack approval** (👍). Approvals expire after
  five minutes; a changed contract, quantity, structure, exit level, or >3% executable-price move
  requires a second approval.
- **Risk gate (`risk.py`) is hard code, not the LLM.** Per current config:
  - ≤ **12%** of the *live* pot per trade (sizing reads NetLiquidation each cycle — it scales as the pot moves; nothing hardcoded)
  - ≤ **4** concurrent positions
  - **−8%** daily circuit breaker → no new entries the rest of the day
  - ≤ **36%** aggregate single-name exposure
  - universe = **SPY / QQQ / IWM** + any liquid single name the model proposes (`allow_model_names: true`) — every entry still needs your explicit 👍
- **Entry kill switch:** create the file at `kill_switch.path` → new entries stop next
  cycle. Protective, risk-reducing, and explicitly requested exits remain armed so the
  switch cannot strand an open position.
- **`TRADING_DOWN` is a second independent entry halt.** Both markers are checked before any daily
  slate/manual entry work and again beside every BUY submission. Missing config, unreadable markers,
  stale/missing NBBO, account errors, unknown earnings, or failed risk checks block the entry.
- **Protective exits run independently every 30 seconds** using static rules; the slower model/entry
  cycle runs every 1,200 seconds. A host-wide order lock serializes all broker mutations.
- The former scheduled `KILL_SWITCH` auto-lift is permanently retired. Re-arming is manual.
- **Audit log:** every proposal, gate decision, approval, and fill is appended to `audit.jsonl`.

## First run (paper)
1. Start IB Gateway/TWS in **paper** mode (API enabled). Default port `7497`.
2. `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
3. Set `SLACK_BOT_TOKEN` in your env; set `slack_channel` + `approver_ids` (your Slack user id) in `config.yaml`.
4. Edit `config.yaml`: `llm_endpoint`/`llm_model`, `approved_names`, and (optional) `pot_cap_usd`.
5. **Dry run** (proposes + posts to Slack, places nothing):
   ```
   python run_trader.py --loop --interval 1200 --protective-interval 30
   ```
   Watch the Slack proposals and `audit.jsonl` for a good while. Confirm the picks and sizing look sane.
6. Tests: `pytest -q` (should be all green).

## Going live (only after paper proves out)
```
python run_trader.py --arm --loop --interval 1200 --protective-interval 30
```
Now each gated proposal is posted for your 👍; approve the ones you want, the rest expire. The exit
manager (`main.py` logic, folded into the loop) manages exits on whatever opens.

## Components
| file | role |
|---|---|
| `exitmgr/strategist.py` | LLM → strict-validated trade ideas |
| `exitmgr/risk.py` | hard risk gate (dynamic, pot-relative caps) |
| `exitmgr/account.py` | live pot value from IBKR (NetLiquidation) |
| `exitmgr/approval.py` | Slack approve-each (reject-wins, approver allowlist, expiry) |
| `exitmgr/trader.py` | orchestrator + audit log + day-start baseline |
| `exitmgr/{connection,order,state,rules,manager}.py` | execution + exit management |
| `exitmgr/byron_evidence.py` | optional point-in-time source capture for Byron replay |

## Byron production-evidence capture

Source capture is deliberately **off by default** and does not change model,
risk, construction, order, or exit behavior. To collect prospective evidence,
set `BYRON_SOURCE_CAPTURE_PATH` to a protected local JSONL path before starting
the trader. The capture mirrors audit events and records IBKR account snapshots,
broker fills, and the source-native timestamp, size, bid, and ask of entry and
exit quotes.

```sh
export BYRON_SOURCE_CAPTURE_PATH="$HOME/protected/byron/exitmgr-source.jsonl"
python run_trader.py --loop --interval 1200 --protective-interval 30
```

If a quote lacks an IBKR timestamp, bid/ask size, or a complete two-sided
market, capture writes `exitmgr-source.jsonl.INVALID.json`. Trading continues,
but the entire evidence run is invalid for portfolio claims. This source stream
is not itself P&L: Byron must join it to the locked calendar, model/runtime/config
receipts, capital reservations, commissions, benchmark tape, and official-close
broker reconciliation, then terminally attest it outside this repository.

## Known surfaces to validate on paper first
- **Option contract selection** in `trader._execute_entry` (chain → expiry near DTE → strike by
  delta → qualify). Broker-specific; watch fills on paper before trusting it live.
- **Market context** in `trader._market_context` is intentionally minimal — add real quotes/signals there.
