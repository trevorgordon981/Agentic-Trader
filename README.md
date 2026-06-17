# exitmgr — LLM-assisted options trading (propose → gate → approve → execute → manage)

A small system where the model **proposes** option swing trades and you **approve every entry**,
while hard-coded risk rails and an exit manager keep the book bounded. Built for a small,
ring-fenced IBKR pot.

> **Money warning.** An LLM has no proven trading edge. Treat the pot as risk capital you are
> prepared to lose. Run on **paper for weeks** and watch the proposals before risking a cent.

## Safety model (read this)
- **Dry-run is the default.** No order is ever placed without `--arm`.
- **Even with `--arm`, every entry needs an explicit Slack approval** (👍). ❌ or no reaction = skipped.
- **Risk gate (`risk.py`) is hard code, not the LLM.** Per current config:
  - ≤ **12%** of the *live* pot per trade (sizing reads NetLiquidation each cycle — it scales as the pot moves; nothing hardcoded)
  - ≤ **4** concurrent positions
  - **−8%** daily circuit breaker → no new entries the rest of the day
  - ≤ **36%** aggregate single-name exposure
  - universe = **SPY / QQQ / IWM** + any liquid single name the model proposes (`allow_model_names: true`) — every entry still needs your explicit 👍
- **Kill switch:** create the file at `kill_switch.path` → all order placement stops next cycle.
- **Audit log:** every proposal, gate decision, approval, and fill is appended to `audit.jsonl`.

## First run (paper)
1. Start IB Gateway/TWS in **paper** mode (API enabled). Default port `7497`.
2. `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`
3. Set `SLACK_BOT_TOKEN` in your env; set `slack_channel` + `approver_ids` (your Slack user id) in `config.yaml`.
4. Edit `config.yaml`: `llm_endpoint`/`llm_model`, `approved_names`, and (optional) `pot_cap_usd`.
5. **Dry run** (proposes + posts to Slack, places nothing):
   ```
   python run_trader.py --loop --interval 900
   ```
   Watch the Slack proposals and `audit.jsonl` for a good while. Confirm the picks and sizing look sane.
6. Tests: `pytest -q` (should be all green).

## Going live (only after paper proves out)
```
python run_trader.py --arm --loop --interval 900
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

## Known surfaces to validate on paper first
- **Option contract selection** in `trader._execute_entry` (chain → expiry near DTE → strike by
  delta → qualify). Broker-specific; watch fills on paper before trusting it live.
- **Market context** in `trader._market_context` is intentionally minimal — add real quotes/signals there.
