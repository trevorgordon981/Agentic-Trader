"""Public API contract; production-derived narrative omitted."""
import json
import math
from collections import namedtuple
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

from exitmgr.rules import days_to_expiry




























DEBIT_SIDE = "debit"
CREDIT_SIDE = "credit"



DEBIT_MIN_DTE_DEFAULT = 25
DEBIT_PREFER_DTE_MAX_DEFAULT = 800
CREDIT_MIN_DTE_DEFAULT = 3
CREDIT_MAX_DTE_DEFAULT = 45

DteBounds = namedtuple("DteBounds", "min_dte prefer_dte_max max_dte")


def _int_or(value, fallback):
    """Public API contract; production-derived narrative omitted."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def normalize_side(side):
    """Public API contract; production-derived narrative omitted."""
    try:
        s = str(side or "").strip().lower()
    except Exception:
        return DEBIT_SIDE
    return CREDIT_SIDE if s == CREDIT_SIDE else DEBIT_SIDE


def dte_bounds_for_side(side, cons=None):
    """Public API contract; production-derived narrative omitted."""
    if normalize_side(side) == CREDIT_SIDE:
        lo = max(1, _int_or(getattr(cons, "credit_min_dte", None), CREDIT_MIN_DTE_DEFAULT))
        hi = _int_or(getattr(cons, "credit_max_dte", None), CREDIT_MAX_DTE_DEFAULT)
        hi = max(lo, hi)
        return DteBounds(lo, hi, hi)

    lo = _int_or(getattr(cons, "min_dte", None), DEBIT_MIN_DTE_DEFAULT)
    if lo <= 0:
        lo = DEBIT_MIN_DTE_DEFAULT
    hi = max(lo, _int_or(getattr(cons, "prefer_dte_max", None), DEBIT_PREFER_DTE_MAX_DEFAULT))
    return DteBounds(lo, hi, None)


def pick_expiry(expirations, target_dte, min_dte=25, prefer_dte_max=45, today=None, max_dte=None):
    """Public API contract; production-derived narrative omitted."""
    t0 = today or datetime.now(timezone.utc).date()
    floor = _int_or(min_dte, DEBIT_MIN_DTE_DEFAULT)


    hard_max = None if max_dte is None else _int_or(max_dte, 0)
    cands = _dte_candidates(expirations, floor, hard_max, t0)
    if not cands:
        return None, None, False
    eff_target, tgt = _effective_target(target_dte, floor, prefer_dte_max, hard_max)
    exp, dte = min(cands, key=lambda x: abs(x[1] - eff_target))
    return exp, dte, (eff_target != tgt)


def _dte_candidates(expirations, floor, hard_max, t0):
    """Public API contract; production-derived narrative omitted."""
    cands = []
    for e in expirations or []:
        try:
            d = (datetime.strptime(str(e)[:8], "%Y%m%d").date() - t0).days
        except (ValueError, TypeError):
            continue
        if d < floor:
            continue
        if hard_max is not None and d > hard_max:
            continue
        cands.append((str(e), d))
    return cands


def _effective_target(target_dte, floor, prefer_dte_max, hard_max=None):
    """Public API contract; production-derived narrative omitted."""
    try:
        tgt = int(target_dte or 0)
    except (TypeError, ValueError):
        tgt = 0
    soft_ceiling = max(_int_or(prefer_dte_max, floor), floor)
    if hard_max is not None:
        soft_ceiling = min(soft_ceiling, max(hard_max, floor))
    return min(max(tgt, floor), soft_ceiling), tgt


def pick_expiry_for_side(expirations, target_dte, side=None, cons=None, today=None):
    """Public API contract; production-derived narrative omitted."""
    b = dte_bounds_for_side(side, cons)
    return pick_expiry(expirations, target_dte, min_dte=b.min_dte,
                       prefer_dte_max=b.prefer_dte_max, today=today, max_dte=b.max_dte)
























TP_BACKSTOP_MIN_PCT = 100.0
TP_BACKSTOP_MAX_PCT = 500.0







TP_POLICY_CURRENT = "explicit-only-v2"


def tp_tier_for_pot(net_liq, tiers, fallback_max, fallback_default):
    """Public API contract; production-derived narrative omitted."""
    if not tiers:
        return fallback_max, fallback_default
    try:
        nl = float(net_liq)
    except (TypeError, ValueError):
        return fallback_max, fallback_default
    if nl != nl:
        return fallback_max, fallback_default
    rows = []
    for t in tiers:
        try:
            rows.append((float(t["min_pot"]), float(t["tp_max_pct"]), float(t["tp_pct"])))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    if not rows:
        return fallback_max, fallback_default
    rows.sort(key=lambda r: r[0])
    chosen = rows[0]
    for mp, tmax, tdef in rows:
        if nl >= mp:
            chosen = (mp, tmax, tdef)
        else:
            break
    return chosen[1], chosen[2]


def clamp_stop_pct(sl_pct, cons):
    """Public API contract; production-derived narrative omitted."""
    sl_def = abs(float(cons.sl_pct)) * 100.0
    try:
        sl_in = float(sl_pct or 0.0)
    except (TypeError, ValueError):
        sl_in = 0.0
    sl = sl_def if sl_in <= 0 else min(sl_def, sl_in)
    sl = max(5.0, sl)  # never a hair-trigger stop from a garbage model value
    return round(sl, 1)


def optional_take_profit_pct(tp_pct):
    """Public API contract; production-derived narrative omitted."""
    if tp_pct is None:
        return None, ""
    try:
        v = float(tp_pct)
    except (TypeError, ValueError):
        return None, f"take_profit refused: non-numeric value {tp_pct!r} (no target installed)"
    if v != v:
        return None, "take_profit refused: NaN (no target installed)"
    if v <= 0:
        return None, ""
    if v < TP_BACKSTOP_MIN_PCT or v > TP_BACKSTOP_MAX_PCT:
        return None, (
            f"take_profit +{v:g}% NOT installed -- doctrine-only take-profit (Sol audit R5 R2 / "
            f"RULING_TAKE_PROFIT.md). A percentage is not an exit signal; a take-profit needs a "
            f"changed thesis AND a giveback from peak. Only an explicit "
            f"{TP_BACKSTOP_MIN_PCT:g}-{TP_BACKSTOP_MAX_PCT:g}% catastrophe backstop may be a "
            f"numeric automatic exit.")
    return round(v, 1), ""


def journal_take_profit_pct(journal_entry):
    """Public API contract; production-derived narrative omitted."""
    je = journal_entry or {}
    try:
        policy = je.get("tp_policy")
    except AttributeError:
        return None
    if policy != TP_POLICY_CURRENT:
        return None
    tp, _note = optional_take_profit_pct(je.get("profit_target_pct"))
    return tp


def clamp_tp_sl(tp_pct, sl_pct, cons):
    """Public API contract; production-derived narrative omitted."""
    tp, _note = optional_take_profit_pct(tp_pct)
    return tp, clamp_stop_pct(sl_pct, cons)




def expected_move(spot, iv, dte):
    """Public API contract; production-derived narrative omitted."""
    try:
        s, v = float(spot or 0), float(iv or 0)
    except (TypeError, ValueError):
        return None
    if s <= 0 or v <= 0 or v != v:
        return None
    return s * v * math.sqrt(max(int(dte or 0), 1) / 365.0)


def effective_delta(target_delta, cons):
    """Public API contract; production-derived narrative omitted."""
    try:
        d = abs(float(target_delta))
    except (TypeError, ValueError):
        d = 0.0
    if d <= 0 or d != d:
        return (float(cons.delta_min) + float(cons.delta_max)) / 2.0
    return max(float(cons.delta_min), min(float(cons.delta_max), d))


def long_strike_ok(strike, spot, right, dte, atm_iv, cons):
    """Public API contract; production-derived narrative omitted."""
    try:
        s, k = float(spot or 0), float(strike or 0)
    except (TypeError, ValueError):
        return False, "INSUFFICIENT_DATA: spot/strike unavailable -- cannot judge lottery-long structure (holding)"
    if s <= 0 or k <= 0:
        return False, "INSUFFICIENT_DATA: spot/strike unavailable -- cannot judge lottery-long structure (holding)"
    otm = (k - s) if right == "C" else (s - k)
    if otm <= 0:
        return True, ""
    em = expected_move(s, atm_iv, dte)
    lim = em if em is not None else float(cons.strike_near_spot_pct) * s
    if otm > lim + 1e-9:
        basis = "~1 expected move" if em is not None else f"{float(cons.strike_near_spot_pct):.0%} of spot (no IV; conservative)"
        return False, (f"long strike {k:g} is ${otm:,.2f} OTM of spot ${s:,.2f} "
                       f"(> {basis} ${lim:,.2f}) -- lottery-ticket structure")
    return True, ""


def spread_structure_ok(long_strike, short_strike, spot, right, dte, atm_iv, cons):
    """Public API contract; production-derived narrative omitted."""
    try:
        s = float(spot or 0)
        lk, sk = float(long_strike or 0), float(short_strike or 0)
    except (TypeError, ValueError):
        return False, "INSUFFICIENT_DATA: spot/strikes unavailable -- cannot judge spread structure (holding)"
    if s <= 0 or lk <= 0 or sk <= 0:
        return False, "INSUFFICIENT_DATA: spot/strikes unavailable -- cannot judge spread structure (holding)"
    em = expected_move(s, atm_iv, dte)
    if em is not None:
        dist = abs(sk - s)
        if dist > em + 1e-9:
            return False, (f"short strike {sk:g} is ${dist:,.2f} from spot ${s:,.2f} "
                           f"(> ~1 expected move ${em:,.2f} for {dte} DTE)")
        return True, ""
    width = abs(sk - lk)
    if width > float(cons.spread_width_max_pct) * s + 1e-9:
        return False, (f"spread width {width:g} > {float(cons.spread_width_max_pct):.0%} of spot "
                       f"${s:,.2f} (no IV available; conservative gate)")
    if abs(lk - s) > float(cons.strike_near_spot_pct) * s + 1e-9:
        return False, (f"long strike {lk:g} > {float(cons.strike_near_spot_pct):.0%} from spot "
                       f"${s:,.2f} (no IV available; conservative gate)")
    return True, ""




def max_premium_budget(net_liq, cons):
    """Public API contract; production-derived narrative omitted."""
    try:
        nl = float(net_liq or 0)
    except (TypeError, ValueError):
        nl = 0.0
    return max(0.0, float(cons.max_premium_pct) * nl)


def check_budget(debit_usd, dte, net_liq, open_book, cons):
    """Public API contract; production-derived narrative omitted."""
    reasons = []
    try:
        nl = float(net_liq or 0)
    except (TypeError, ValueError):
        nl = 0.0
    if nl <= 0:
        return True, []
    debit = float(debit_usd or 0)
    try:
        d = int(dte or 0)
    except (TypeError, ValueError):
        d = 0
    prem_cap = float(cons.max_premium_pct) * nl
    if debit > prem_cap + 1e-6:
        reasons.append(f"premium ${debit:,.0f} > {float(cons.max_premium_pct):.0%}-of-net-liq cap ${prem_cap:,.0f}")
    deployed = sum(float(b or 0) for b, _ in (open_book or []))
    dep_cap = float(cons.max_deployed_pct) * nl
    if deployed + debit > dep_cap + 1e-6:
        reasons.append(f"total deployed premium ${deployed + debit:,.0f} (open ${deployed:,.0f} + this ${debit:,.0f}) "
                       f"> {float(cons.max_deployed_pct):.0%}-of-net-liq cap ${dep_cap:,.0f}")



    decay = (debit / d) / nl if d > 0 else 0.0
    if d > 0 and decay > float(cons.max_decay_pct_per_day) + 1e-9:
        reasons.append(f"theta-decay budget {decay:.2%}/day (${debit:,.0f} over {d} DTE) "
                       f"> {float(cons.max_decay_pct_per_day):.1%}/day per-trade cap")
    port_decay = decay + sum((float(b or 0) / int(x)) for b, x in (open_book or [])
                             if x and int(x) > 0) / nl
    if port_decay > float(cons.max_portfolio_decay_pct_per_day) + 1e-9:
        reasons.append(f"portfolio theta-decay {port_decay:.2%}/day would exceed the "
                       f"{float(cons.max_portfolio_decay_pct_per_day):.0%}/day total cap")
    return (not reasons), reasons


def _read_journal_lines(journal_path):
    """Public API contract; production-derived narrative omitted."""
    out = []
    p = Path(journal_path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _load_journal_rows(journal_path):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr.manager import build_journal_campaigns, open_campaign

    rows = {}
    rows_by_ref = {}
    entries = _read_journal_lines(journal_path)
    try:
        campaigns = build_journal_campaigns(entries)
    except Exception:
        campaigns = {}
    for cid in campaigns:
        camp = open_campaign(campaigns, cid)
        if camp is None or not camp.lots:
            continue
        debit = camp.total("debit")
        if debit is None:
            continue
        newest = camp.lots[-1].row
        try:
            row = (float(debit), newest.get("expiry"), newest.get("decision_id"), int(cid))
        except (TypeError, ValueError):
            continue
        rows[int(cid)] = row




        for lot in camp.lots:
            ref = lot.row.get("order_ref")
            if ref:
                rows_by_ref[str(ref)] = row
    return rows, rows_by_ref


def open_book_items(positions, journal_path, open_orders=None):
    """Public API contract; production-derived narrative omitted."""
    rows, rows_by_ref = _load_journal_rows(journal_path)
    items = {}
    for cid, pos in (positions or {}).items():
        r = rows.get(int(cid))
        if r is not None:
            debit, exp, decision_id, _ = r
            dte = days_to_expiry(exp)
            key = f"decision:{decision_id}" if decision_id else f"position:{int(cid)}"
        else:
            debit = abs(getattr(pos, "avg_cost", 0.0)) * 100 * abs(getattr(pos, "quantity", 0))
            dte = days_to_expiry(getattr(pos, "expiry", ""))
            key = f"position:{int(cid)}"
        items[key] = (float(debit), dte if (dte and dte > 0) else 0)

    terminal = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
    for trade in open_orders or []:
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        status = getattr(getattr(trade, "orderStatus", None), "status", None)
        if order is None or contract is None or str(getattr(order, "action", "")).upper() != "BUY":
            continue
        if status in terminal:
            continue
        ref = str(getattr(order, "orderRef", "") or "")
        row = rows_by_ref.get(ref)
        if row is not None:
            debit, exp, decision_id, long_cid = row
            key = f"decision:{decision_id}" if decision_id else f"position:{long_cid}"
            dte = days_to_expiry(exp)
        else:
            price = getattr(order, "lmtPrice", 0) or 0
            remaining = getattr(getattr(trade, "orderStatus", None), "remaining", None)
            qty = remaining if isinstance(remaining, (int, float)) and remaining > 0 \
                else (getattr(order, "totalQuantity", 0) or 0)
            try:
                debit = float(price) * 100 * float(qty)
            except (TypeError, ValueError):
                debit = 0.0
            dte = days_to_expiry(getattr(contract, "lastTradeDateOrContractMonth", ""))
            oid = getattr(order, "permId", None) or getattr(order, "orderId", None) or ref
            key = f"order:{oid}"
        items[key] = (float(debit), dte if (dte and dte > 0) else 0)
    return items


class _OpenOrdersOmitted:
    """Public API contract; production-derived narrative omitted."""

    __slots__ = ()

    def __repr__(self):
        return "<open_orders not supplied>"


_OPEN_ORDERS_OMITTED = _OpenOrdersOmitted()


def open_book(positions, journal_path, open_orders=_OPEN_ORDERS_OMITTED):
    """Public API contract; production-derived narrative omitted."""
    if open_orders is _OPEN_ORDERS_OMITTED:
        raise TypeError(
            "construction.open_book() requires open_orders. Without it the working-BUY fold "
            "is skipped, so submitted-but-unfilled entries count as $0 of deployed premium "
            "and check_budget's max_deployed_pct cap under-counts by exactly the orders in "
            "flight. Pass reqAllOpenOrdersAsync() output, or pass an explicit [] to state on "
            "the record that there are none.")
    return list(open_book_items(positions, journal_path, open_orders).values())




def _as_date(x):
    """Public API contract; production-derived narrative omitted."""
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, _date):
        return x
    s = str(x).strip()
    if not s:
        return None

    digits = s[:8]
    if len(digits) == 8 and digits.isdigit():
        try:
            return datetime.strptime(digits, "%Y%m%d").date()
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def earnings_ok(entry_date, expiry, earnings_date, cons, hold_days=None):
    """Public API contract; production-derived narrative omitted."""
    if earnings_date is None:
        return True, ""
    ea = _as_date(earnings_date)
    xp = _as_date(expiry)
    en = _as_date(entry_date)
    if ea is None or xp is None:
        return True, ""
    if en is not None and ea == en:
        return True, (
            f"EARNINGS TODAY ({ea.isoformat()}) — SAME-DAY BINARY EVENT WARNING: "
            "release timing/status is unverified from the calendar date alone; treat the event "
            "as UPCOMING unless authoritative evidence gives a reported timestamp before this "
            "decision. Entry remains allowed. Before the release, the thesis must underwrite an "
            "overnight gap beyond ordinary risk controls and structure-specific downside, plus "
            "post-print IV repricing; after a verified release, it must reassess the actual price "
            "reaction, fresh option IV/NBBO, and stabilization"
        )
    if not bool(getattr(cons, "earnings_blackout_enabled", True)):
        return True, ""
    if en is not None and ea < en:
        return True, ""
    try:
        buf = int(getattr(cons, "earnings_blackout_days", 0) or 0)
    except (TypeError, ValueError):
        buf = 0
    cutoff = xp + timedelta(days=max(0, buf))
    window = f"on/before expiry {xp.isoformat()}" if buf == 0 else \
             f"on/before expiry {xp.isoformat()} +{buf}d cushion ({cutoff.isoformat()})"






    if bool(getattr(cons, "earnings_use_hold_window", False)) and en is not None:
        try:
            _hold = int(hold_days) if hold_days is not None else None
        except (TypeError, ValueError):
            _hold = None
        if _hold and _hold > 0:
            try:
                _mult = float(getattr(cons, "earnings_hold_slip_mult", 2.0) or 2.0)
            except (TypeError, ValueError):
                _mult = 2.0
            _mult = max(1.0, _mult)
            _hold_cut = en + timedelta(days=int(round(_hold * _mult)) + max(0, buf))
            if _hold_cut < cutoff:

                cutoff = _hold_cut
                window = (f"within {_hold}d intended hold x{_mult:g} slip "
                          f"(through {cutoff.isoformat()}; expiry {xp.isoformat()})")

    if ea <= cutoff:
        reason = (f"earnings {ea.isoformat()} falls within the holding horizon ({window}); "
                  "the event can cause a sharp gap and post-print IV crush")
        if bool(getattr(cons, "earnings_block_hard", False)):
            return False, reason + " -- hard earnings gate enabled"
        return True, reason + " -- disclosed warning only; holding through earnings is allowed"
    return True, ""




def assignment_risk_ok(short_strike, spot, right, expiry, ex_div_date, dte, cons):
    """Public API contract; production-derived narrative omitted."""
    if not bool(getattr(cons, "assignment_check_enabled", True)):
        return True, ""
    try:
        k = float(short_strike or 0)
    except (TypeError, ValueError):
        return True, ""
    if k <= 0:
        return True, ""
    try:
        s = float(spot or 0)
    except (TypeError, ValueError):
        return True, ""
    if s <= 0:
        return True, ""
    if ex_div_date is None:
        return True, ""
    xd = _as_date(ex_div_date)
    xp = _as_date(expiry)
    if xd is None or xp is None:
        return True, ""
    itm = (right == "C" and s >= k) or (right == "P" and s <= k)
    if not itm:
        return True, ""
    try:
        buf = int(getattr(cons, "assignment_cushion_days", 0) or 0)
    except (TypeError, ValueError):
        buf = 0
    cutoff = xp + timedelta(days=max(0, buf))
    if xd > cutoff:
        return True, ""
    window = f"on/before expiry {xp.isoformat()}" if buf == 0 else \
             f"on/before expiry {xp.isoformat()} +{buf}d cushion ({cutoff.isoformat()})"
    reason = (f"early-assignment risk: ITM short {right} {k:g} (spot {s:g}) heading into "
              f"ex-dividend {xd.isoformat()} ({window}) -- the short leg may be exercised early "
              f"to capture the dividend, converting the spread")
    if bool(getattr(cons, "assignment_block_hard", False)):
        return False, reason
    return True, reason





DOCTRINE_HOLD_MULTIPLE = 8
DOCTRINE_LONG_DELTA = 0.60
DOCTRINE_MAX_DTE = 800
DOCTRINE_MIN_HOLD = 5


def deterministic_expiry(intended_hold_days, min_dte, max_dte=DOCTRINE_MAX_DTE):
    """Public API contract; production-derived narrative omitted."""
    try:
        hold = int(intended_hold_days)
    except (TypeError, ValueError):
        return None
    if hold < DOCTRINE_MIN_HOLD:
        return None
    dte = hold * DOCTRINE_HOLD_MULTIPLE
    return int(max(int(min_dte), min(int(max_dte), dte)))


def apply_deterministic_construction(idea, min_dte, enabled=True):
    """Public API contract; production-derived narrative omitted."""
    if not enabled or idea is None:
        return idea
    if str(getattr(idea, "side", "debit") or "debit").lower() == "credit":
        return idea

    dte = deterministic_expiry(getattr(idea, "intended_hold_days", None), min_dte)
    if dte is None:
        return idea


    if not hasattr(idea, "model_target_dte") or idea.model_target_dte is None:
        try:
            object.__setattr__(idea, "model_target_dte", getattr(idea, "target_dte", None))
            object.__setattr__(idea, "model_target_delta", getattr(idea, "target_delta", None))
        except Exception:
            pass
    try:
        object.__setattr__(idea, "target_dte", dte)
        object.__setattr__(idea, "target_delta", DOCTRINE_LONG_DELTA)
    except Exception:
        return idea
    return idea








































ConstructionOutcome = namedtuple("ConstructionOutcome", "ideas dropped changes")


def apply_construction_policy(ideas, *, min_dte, enabled=True):
    """Public API contract; production-derived narrative omitted."""
    kept, dropped, changes = [], [], []
    floor = None
    if enabled:
        try:
            floor = int(DEBIT_MIN_DTE_DEFAULT if min_dte is None else min_dte)
        except (TypeError, ValueError):
            floor = None
    for idea in (ideas or []):
        if idea is None:
            continue
        if not enabled:
            kept.append(idea)
            continue
        if floor is None:
            dropped.append((idea,
                            "deterministic construction is enabled but construction.min_dte is "
                            "unusable (%r), so the rule cannot be applied" % (min_dte,)))
            continue
        before_dte = getattr(idea, "target_dte", None)
        before_delta = getattr(idea, "target_delta", None)



        _side = getattr(idea, "side", None)
        _side = DEBIT_SIDE if _side is None else _side
        is_credit_side = str(_side).lower() == CREDIT_SIDE
        rule_dte = (None if is_credit_side else
                    deterministic_expiry(getattr(idea, "intended_hold_days", None), floor))
        try:
            apply_deterministic_construction(idea, floor, enabled=True)
        except Exception as exc:
            dropped.append((idea, "deterministic construction raised (%s: %s)"
                                  % (type(exc).__name__, exc)))
            continue
        after_dte = getattr(idea, "target_dte", None)
        after_delta = getattr(idea, "target_delta", None)
        if rule_dte is not None and (after_dte != rule_dte or after_delta != DOCTRINE_LONG_DELTA):
            dropped.append((idea,
                            "deterministic construction did not take: the rule requires "
                            "target_dte=%r target_delta=%r, the idea carries %r/%r"
                            % (rule_dte, DOCTRINE_LONG_DELTA, after_dte, after_delta)))
            continue
        if before_dte != after_dte or before_delta != after_delta:
            changes.append({
                "underlying": getattr(idea, "underlying", None),
                "intended_hold_days": getattr(idea, "intended_hold_days", None),
                "model_target_dte": before_dte,
                "rule_target_dte": after_dte,
                "model_target_delta": before_delta,
                "rule_target_delta": after_delta,
            })
        kept.append(idea)
    return ConstructionOutcome(tuple(kept), tuple(dropped), tuple(changes))


























def stage_b_quote_max_age_s(cons):
    """Public API contract; production-derived narrative omitted."""
    from exitmgr import entry_safety
    default = float(entry_safety.DEFAULT_NBBO_MAX_AGE_SECONDS)
    try:
        value = float(getattr(cons, "stage_b_quote_max_age_s", None))
    except (TypeError, ValueError):
        return default
    if value != value or value <= 0.0 or value == float("inf"):
        return default
    return value

































DOCTRINE_MIN_HOLD_MULTIPLE = 5

MAX_POSITIONS_PER_EXPIRY_DEFAULT = 1





ExpiryChoice = namedtuple(
    "ExpiryChoice", "expiry dte adjusted shifted from_expiry from_dte reason")


def _normalize_expiry(x):
    """Public API contract; production-derived narrative omitted."""
    d = _as_date(x)
    return d.strftime("%Y%m%d") if d is not None else None


def open_expiry_counts(positions, journal_path, open_orders=None):
    """Public API contract; production-derived narrative omitted."""
    try:
        rows, rows_by_ref = _load_journal_rows(journal_path)
        keyed = {}
        for cid, pos in (positions or {}).items():
            try:
                cid_i = int(cid)
            except (TypeError, ValueError):
                continue
            r = rows.get(cid_i)
            if r is not None:
                _debit, exp, decision_id, _ = r
                key = f"decision:{decision_id}" if decision_id else f"position:{cid_i}"
            else:
                exp = getattr(pos, "expiry", None)
                key = f"position:{cid_i}"
            e = _normalize_expiry(exp)
            if e:
                keyed[key] = e
        terminal = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
        for trade in open_orders or []:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            status = getattr(getattr(trade, "orderStatus", None), "status", None)
            if order is None or contract is None \
                    or str(getattr(order, "action", "")).upper() != "BUY":
                continue
            if status in terminal:
                continue
            ref = str(getattr(order, "orderRef", "") or "")
            row = rows_by_ref.get(ref)
            if row is not None:
                _debit, exp, decision_id, long_cid = row
                key = f"decision:{decision_id}" if decision_id else f"position:{long_cid}"
            else:
                exp = getattr(contract, "lastTradeDateOrContractMonth", "")
                oid = getattr(order, "permId", None) or getattr(order, "orderId", None) or ref
                key = f"order:{oid}"
            e = _normalize_expiry(exp)
            if e:
                keyed[key] = e
        counts = {}
        for e in keyed.values():
            counts[e] = counts.get(e, 0) + 1
        return counts
    except Exception:
        return {}


def _as_expiry_counts(open_expiries):
    """Public API contract; production-derived narrative omitted."""
    counts = {}
    if not open_expiries:
        return counts
    try:
        if isinstance(open_expiries, dict):
            for k, v in open_expiries.items():
                e = _normalize_expiry(k)
                if not e:
                    continue
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    n = 0
                if n > 0:
                    counts[e] = counts.get(e, 0) + n
            return counts
        for x in open_expiries:
            e = _normalize_expiry(x)
            if e:
                counts[e] = counts.get(e, 0) + 1
    except Exception:
        return {}
    return counts


def resolve_max_per_expiry(cons=None, max_per_expiry=None):
    """Public API contract; production-derived narrative omitted."""
    v = max_per_expiry
    if v is None:
        v = getattr(cons, "max_positions_per_expiry", None)
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = MAX_POSITIONS_PER_EXPIRY_DEFAULT
    return max(1, n)


def diversify_expiry(expiry, dte, expirations, target_dte, open_expiries=None, side=None,
                     cons=None, today=None, max_per_expiry=None):
    """Public API contract; production-derived narrative omitted."""
    try:
        if not expiry or dte is None:

            return ExpiryChoice(expiry, dte, False, False, expiry, dte, "")
        cap = resolve_max_per_expiry(cons, max_per_expiry)
        counts = _as_expiry_counts(open_expiries)
        here = _normalize_expiry(expiry)
        held = counts.get(here, 0) if here else 0
        if held < cap:
            return ExpiryChoice(expiry, dte, False, False, expiry, dte,
                                f"expiry {here or expiry} hosts {held} open position(s), "
                                f"within the {cap}-per-expiry cap")

        b = dte_bounds_for_side(side, cons)
        t0 = today or datetime.now(timezone.utc).date()
        eff_target, _raw = _effective_target(target_dte, b.min_dte, b.prefer_dte_max, b.max_dte)



        band_lo = max(int(b.min_dte),
                      int(math.ceil(eff_target * DOCTRINE_MIN_HOLD_MULTIPLE
                                    / float(DOCTRINE_HOLD_MULTIPLE))))
        band_lo = min(band_lo, int(dte))
        band_hi = max(int(eff_target), int(dte))

        best = None
        for e, d in _dte_candidates(expirations, b.min_dte, b.max_dte, t0):
            if d < band_lo or d > band_hi:
                continue
            key = _normalize_expiry(e)
            if key is None or key == here:
                continue
            n = counts.get(key, 0)
            rank = (n, abs(d - eff_target), d)
            if best is None or rank < best[0]:
                best = (rank, e, d, n)
        if best is None:
            return ExpiryChoice(expiry, dte, False, False, expiry, dte,
                                f"expiry {here or expiry} hosts {held} open position(s) "
                                f"(cap {cap}) but no alternative expiry exists inside the "
                                f"{band_lo}-{band_hi} DTE doctrine window -- kept")
        _rank, alt_e, alt_d, alt_n = best
        if alt_n >= held:
            return ExpiryChoice(expiry, dte, False, False, expiry, dte,
                                f"expiry {here or expiry} hosts {held} open position(s) "
                                f"(cap {cap}) but every alternative in the {band_lo}-{band_hi} "
                                f"DTE window is at least as crowded ({alt_n}) -- kept")
        return ExpiryChoice(
            alt_e, alt_d, False, True, expiry, dte,
            f"expiry diversification: {here or expiry} already hosts {held} open position(s) "
            f"(cap {cap}); shifted to {_normalize_expiry(alt_e) or alt_e} at {alt_d} DTE "
            f"(hosts {alt_n}), inside the {band_lo}-{band_hi} DTE doctrine window "
            f"(target {eff_target})")
    except Exception:
        return ExpiryChoice(expiry, dte, False, False, expiry, dte,
                            "expiry diversification skipped (internal error) -- kept")


def pick_expiry_diversified(expirations, target_dte, side=None, cons=None, today=None,
                            open_expiries=None, max_per_expiry=None):
    """Public API contract; production-derived narrative omitted."""
    expiry, dte, adjusted = pick_expiry_for_side(expirations, target_dte, side=side,
                                                 cons=cons, today=today)
    if expiry is None:
        return ExpiryChoice(None, None, adjusted, False, None, None, "")
    c = diversify_expiry(expiry, dte, expirations, target_dte, open_expiries=open_expiries,
                         side=side, cons=cons, today=today, max_per_expiry=max_per_expiry)
    return ExpiryChoice(c.expiry, c.dte, adjusted, c.shifted, expiry, dte, c.reason)
