#!/usr/bin/env python3
"""test_e2e_gemma.py -- end-to-end smoke test for the LIVE trading path that will use the
fine-tuned Gemma directional model.

The fine-tuned Gemma-4-31B (training finishes Saturday) consumes a BYTE-EXACT technical card and
emits {"call":"BULLISH|BEARISH|NEUTRAL","conviction":1-10}. This harness exercises the WHOLE chain
for a real ticker so we can prove it works before go-live:

  Stage 1  build the byte-exact technical card (real bars + real ^VIX via yfinance) and assert
           it matches the training format (the same string gen_train_huge3.py emitted).
  Stage 2  send that card to a Gemma endpoint (default: a LOCAL MOCK that returns a valid
           OpenAI chat-completion) via the REAL strategist.gemma_signal(), and assert the
           round-trip parses to {ticker, call, conviction}.
  Stage 3  convert the directional signal -> a TradeIdea, run it through daily_recommend._resolve
           (the real structure/sizing path) with IBKR pricing STUBBED (no market hours, no IBKR,
           no trades), and assert a BULLISH/conv-7 signal yields a sane long-call / debit-spread
           ResolvedOrder.

NOTHING here touches live config, places trades, or restarts services. It only READS config.yaml
(for the model name, to detect _is_gemma) and builds a card from public yfinance data.

-----------------------------------------------------------------------------------------------
SATURDAY GO-LIVE (real end-to-end against the served Gemma):
  Once the Gemma is served (OpenAI-compatible /v1/chat/completions), run:

      ~/ib-grader-venv/bin/python ~/exitmgr-app/test_e2e_gemma.py \
          --endpoint http://127.0.0.1:<GEMMA_PORT>/v1/chat/completions \
          --model <gemma-model-name-or-path>     # must contain 'gemma' so _is_gemma() fires

  That is the ONLY change: point --endpoint at the real server and --model at the served name.
  Stages 1 and 3 are identical; Stage 2 then hits the REAL model instead of the mock and asserts
  the live model returns a parseable call+conviction for the live card. (Stage 3 pricing stays
  stubbed unless you also pass --live-ibkr during market hours -- see below.)
-----------------------------------------------------------------------------------------------
Run (mock, anytime):  ~/ib-grader-venv/bin/python ~/exitmgr-app/test_e2e_gemma.py
"""
import argparse
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.expanduser("~/exitmgr-app"))

from exitmgr.technical_card import fetch_card, card_messages, InsufficientHistory, SYS_T
from exitmgr.strategist import gemma_signal, _is_gemma, TradeIdea
from exitmgr.risk import INDEX_UNDERLYINGS

# ----------------------------------------------------------------------------- pretty PASS/FAIL
_PASS = "\033[92mPASS\033[0m"
_FAIL = "\033[91mFAIL\033[0m"
_results = []


def stage(name, ok, detail=""):
    _results.append((name, ok))
    print(f"  [{_PASS if ok else _FAIL}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


# The format the fine-tune trained on (gen_train_huge3.py `base`). A latest-bar card must:
#   - start with "<TICKER> $<price>. Mom 5d ..."
#   - contain RSI14 / vs SMA20.../SMA50.../SMA200.. / SMA20{>|<}SMA50 / MACD-h / BB%B / ATR / Vol
#   - contain the "+N% from 52wk high, +N% above 52wk low" pair
#   - end with "IVR NN. VIX NN <regime>. Stance next <label>?"
_CARD_RE = re.compile(
    r"^[A-Z.]{1,6} \$[\d,]+\.\d{2}\. "
    r"Mom 5d [+-]\d+\.\d% 20d [+-]\d+\.\d% 60d [+-]\d+\.\d% 120d [+-]\d+\.\d%\. "
    r"RSI14 \d+\. vs SMA20 [+-]\d+\.\d% SMA50 [+-]\d+\.\d% SMA200 [+-]\d+\.\d%\. "
    r"SMA20[<>]SMA50\. MACD-h [+-]\d+\.\d{2}\. BB%B [+-]?\d+\.\d{2}\. ATR \d+\.\d%\. "
    r"Vol \d+\.\d+x\. [+-]\d+% from 52wk high, [+-]\d+% above 52wk low\. "
    r"IVR \d+\. VIX \d+ (calm|normal|elevated|high|extreme)\. "
    r"Stance next (~1 week|~2 weeks|~1 month|~2 months|~3 months)\?$"
)


# --------------------------------------------------------------------------- mock Gemma endpoint
class _MockGemmaHandler(BaseHTTPRequestHandler):
    """Returns a valid OpenAI chat-completion whose content is the trained JSON the real Gemma
    would emit. Captures the LAST request body so the test can assert what was actually sent."""
    captured = {}

    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).captured = body
        content = json.dumps({"call": "BULLISH", "conviction": 7})
        resp = {"choices": [{"message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}]}
        out = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def start_mock():
    srv = HTTPServer(("127.0.0.1", 0), _MockGemmaHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port}/v1/chat/completions", "gemma-mock-ft"


# --------------------------------------------------- Stage 3: signal -> TradeIdea -> _resolve
def signal_to_idea(sig, horizon_label="~2 weeks", dte=14, delta=0.45, est_debit=180.0):
    """Convert the Gemma directional signal dict into a canonical TradeIdea so it can enter the
    SAME structure/sizing path the slate uses (daily_recommend._resolve).

    NOTE (gap found -- see report): this converter does NOT exist in the live code yet. The
    strategist docstring says daily_recommend.main() should, when _is_gemma() is true, call
    gemma_signal() per name and 'feed that into the same structure/pricing path' -- but no code
    does the signal->TradeIdea bridge. This is the missing glue. Structure defaults to a long
    call/put; flip to a debit spread to test that branch.
    """
    call = sig["call"]
    if call == "NEUTRAL":
        return None  # no directional trade
    direction = "bullish" if call == "BULLISH" else "bearish"
    u = sig["ticker"].upper()
    return TradeIdea(
        underlying=u,
        is_index=u in INDEX_UNDERLYINGS,
        direction=direction,
        structure="long call" if direction == "bullish" else "long put",
        target_dte=dte,
        target_delta=delta,
        est_debit_usd=est_debit,
        conviction=sig["conviction"],
        thesis=f"Gemma technical-card signal: {call} conv {sig['conviction']} ({horizon_label}).",
    )


# Minimal fakes so daily_recommend._resolve runs WITHOUT IBKR / market hours / placing anything.
class _FakeContract:
    def __init__(self, strike, right):
        self.strike = strike
        self.right = right
        self.conId = int(strike * 100) + (1 if right == "C" else 2)


class _FakeGreeks:
    def __init__(self, delta):
        self.delta = delta


class _FakeTicker:
    def __init__(self, contract, mid, delta):
        self.contract = contract
        self.bid = round(mid * 0.98, 2)
        self.ask = round(mid * 1.02, 2)
        self.last = mid
        self.modelGreeks = _FakeGreeks(delta)
        self.lastGreeks = None


class _FakeChainParam:
    def __init__(self, spot):
        self.exchange = "SMART"
        self.tradingClass = ""
        # a strike grid around a stubbed spot
        base = round(spot)
        self.strikes = [float(base + i) for i in range(-10, 11)]
        # one expiry ~target DTE out and a couple others
        import datetime as _dt
        today = _dt.date.today()
        self.expirations = [(today + _dt.timedelta(days=d)).strftime("%Y%m%d")
                            for d in (7, 14, 30, 45)]


class _FakeIB:
    """Stubs exactly the IB calls _resolve makes: qualifyContractsAsync, reqSecDefOptParamsAsync,
    reqTickersAsync. underlying_price is monkeypatched separately. No network, no orders."""
    SPOT = 100.0

    async def qualifyContractsAsync(self, *contracts):
        out = []
        for c in contracts:
            if not hasattr(c, "strike"):       # a Stock -> give it a conId
                c.conId = 111111
                out.append(c)
            else:
                out.append(c)
        return out

    async def reqSecDefOptParamsAsync(self, underlying, exch, sectype, conid):
        return [_FakeChainParam(self.SPOT)]

    async def reqTickersAsync(self, *contracts):
        # delta falls off as strike moves above spot (call) -- gives _resolve a delta to pick by.
        out = []
        for c in contracts:
            k = float(getattr(c, "strike", 0) or 0)
            right = getattr(c, "right", "C")
            moneyness = (self.SPOT - k) if right == "C" else (k - self.SPOT)
            delta = max(0.05, min(0.95, 0.5 + moneyness * 0.05))
            mid = max(0.30, 3.0 + moneyness * 0.20)  # intrinsic-ish, always > 0
            out.append(_FakeTicker(c, round(mid, 2), round(delta, 2)))
        return out


async def run_resolve(idea, available, spot=100.0):
    """Call the REAL daily_recommend._resolve with IB + underlying_price + Option/Stock/pick_chain
    stubbed so it needs no IBKR and no market hours. Returns (ResolvedOrder|None, why)."""
    import daily_recommend as dr

    _FakeIB.SPOT = spot

    # patch the IB-facing helpers _resolve imports at call time
    async def _fake_underlying_price(ib, stk):
        return spot

    def _fake_pick_chain(params, underlying):
        return params[0]

    def _fake_strikes_near(strikes, spot_):
        return [k for k in strikes if abs(k - spot_) <= 10]

    class _FakeStock:
        def __init__(self, *a, **k):
            self.symbol = a[0] if a else ""

    class _FakeOption:
        def __init__(self, underlying, expiry, strike, right, exch):
            self.symbol = underlying
            self.strike = float(strike)
            self.right = right
            self.lastTradeDateOrContractMonth = expiry
            self.conId = int(strike * 100) + (1 if right == "C" else 2)

    saved = {k: getattr(dr, k) for k in
             ("underlying_price", "pick_chain", "strikes_near", "Stock", "Option")}
    dr.underlying_price = _fake_underlying_price
    dr.pick_chain = _fake_pick_chain
    dr.strikes_near = _fake_strikes_near
    dr.Stock = _FakeStock
    dr.Option = _FakeOption
    try:
        return await dr._resolve(_FakeIB(), idea, available)
    finally:
        for k, v in saved.items():
            setattr(dr, k, v)


# --------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", default="SPY", help="ticker to build the card for (default SPY)")
    ap.add_argument("--endpoint", default="", help="REAL Gemma endpoint (Saturday). Empty = mock.")
    ap.add_argument("--model", default="", help="REAL Gemma model name (must contain 'gemma').")
    ap.add_argument("--horizon", default="~2 weeks")
    ap.add_argument("--structure", default="long", choices=["long", "spread"],
                    help="Stage 3 structure to exercise (default long call/put).")
    args = ap.parse_args()

    print(f"\n=== E2E Gemma trading-path test  (ticker={args.ticker}, horizon={args.horizon}) ===\n")

    use_mock = not args.endpoint
    srv = None
    if use_mock:
        srv, endpoint, model = start_mock()
        print(f"  (using LOCAL MOCK Gemma at {endpoint})\n")
    else:
        endpoint, model = args.endpoint, args.model or "gemma-ft"
        print(f"  (using REAL endpoint {endpoint}, model {model})\n")

    # sanity: the configured/passed model must be detected as Gemma for the live branch to fire.
    stage("model name routes to Gemma path (_is_gemma)", _is_gemma(model),
          f"model={model!r}")

    # ----- Stage 1: byte-exact technical card -----
    card = None
    try:
        card = fetch_card(args.ticker, horizon_label=args.horizon)
        ok = bool(_CARD_RE.match(card))
        stage("Stage 1: card built & matches training format", ok,
              card[:70] + " ..." if card else "")
        if not ok:
            print(f"\n      CARD: {card}\n")
    except InsufficientHistory as e:
        stage("Stage 1: card built & matches training format", False, f"InsufficientHistory: {e}")
    except Exception as e:
        stage("Stage 1: card built & matches training format", False, f"{type(e).__name__}: {e}")

    # ----- Stage 2: card -> Gemma endpoint -> parsed signal -----
    sig = None
    if card is not None:
        try:
            sig = gemma_signal(endpoint, model, args.ticker, horizon_label=args.horizon, timeout=120)
            ok = (sig is not None
                  and sig["ticker"] == args.ticker.upper()
                  and sig["call"] in ("BULLISH", "BEARISH", "NEUTRAL")
                  and 1 <= sig["conviction"] <= 10)
            stage("Stage 2: gemma_signal round-trip parses",
                  ok, json.dumps({k: sig[k] for k in ("ticker", "call", "conviction")}) if sig else "None")
            # verify the endpoint actually received the byte-exact card + trained system prompt
            if use_mock:
                cap = _MockGemmaHandler.captured
                msgs = cap.get("messages", [])
                sent_user = msgs[-1]["content"] if msgs else ""
                sent_sys = msgs[0]["content"] if msgs else ""
                stage("Stage 2b: endpoint received byte-exact card as user turn",
                      sent_user == sig["card"] if sig else False)
                stage("Stage 2c: endpoint received the trained system prompt",
                      sent_sys == SYS_T.format(label=args.horizon))
        except Exception as e:
            stage("Stage 2: gemma_signal round-trip parses", False, f"{type(e).__name__}: {e}")
    else:
        stage("Stage 2: gemma_signal round-trip parses", False, "skipped (no card)")

    # ----- Stage 3: signal -> TradeIdea -> structure/sizing (_resolve, IBKR stubbed) -----
    if sig and sig["call"] != "NEUTRAL":
        import asyncio
        idea = signal_to_idea(sig, horizon_label=args.horizon)
        if args.structure == "spread":
            idea.structure = "call debit spread" if idea.direction == "bullish" else "put debit spread"
        stage("Stage 3a: signal -> TradeIdea conversion",
              idea is not None and idea.direction in ("bullish", "bearish")
              and idea.conviction == sig["conviction"],
              f"{idea.direction} {idea.structure} conv{idea.conviction}")
        try:
            resolved, why = asyncio.run(run_resolve(idea, available=1010.0, spot=100.0))
            ok = resolved is not None
            detail = why or ""
            if resolved is not None:
                from exitmgr.trader import order_summary
                detail = order_summary(resolved)
                # sanity: right matches direction; qty>=1; debit fits the budget
                right_ok = resolved.right == ("C" if idea.direction == "bullish" else "P")
                size_ok = resolved.qty >= 1 and resolved.limit * 100 * resolved.qty <= 1010.0 + 1e-6
                ok = ok and right_ok and size_ok
                if args.structure == "spread":
                    # spread branch should usually attach a short leg (cheaper); not fatal if it
                    # fell back to single, but report it.
                    detail += "  [spread leg]" if resolved.short_contract is not None else "  [fell back to single]"
            stage("Stage 3b: _resolve -> sane ResolvedOrder (IBKR stubbed)", ok, detail)
        except Exception as e:
            import traceback
            stage("Stage 3b: _resolve -> sane ResolvedOrder (IBKR stubbed)", False,
                  f"{type(e).__name__}: {e}")
            traceback.print_exc()
    elif sig and sig["call"] == "NEUTRAL":
        stage("Stage 3: structure/sizing", True, "NEUTRAL signal -> no trade (correct, skipped)")
    else:
        stage("Stage 3: structure/sizing", False, "skipped (no signal)")

    if srv:
        srv.shutdown()

    # ------------------------------------------------------------------- summary
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"\n=== {passed}/{total} stages PASSED ===")
    if passed != total:
        print("  FAILURES:")
        for name, ok in _results:
            if not ok:
                print(f"    - {name}")
    print()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
