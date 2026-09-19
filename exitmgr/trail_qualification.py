"""Public API contract; production-derived narrative omitted."""
from __future__ import annotations

import math
from datetime import date, datetime, timezone


def _positive(value):
    try:
        x = float(value)
        return x if math.isfinite(x) and x > 0 else None
    except (TypeError, ValueError):
        return None


def qualify(*, con_id, quotes, entry_debit, quantity, journal, atr_record,
            now_monotonic, today, now_utc=None, max_quote_age_s=30., max_quote_skew_s=2.,
            max_atr_age_days=4, k_arm=1., ceiling_pct=20., giveback_fraction=.5):
    out = dict(qualified=False, quote_qualified=False, reason=None,
               executable_price=None, activation_gain_pct=None,
               entry_commission_known=False, exit_commission_known=False,
               fees_note="exit commission unknown; not a net-profit guarantee")
    def block(reason):
        out['reason'] = reason
        return out
    basis = _positive(entry_debit)
    qty = _positive(quantity)
    if basis is None or qty is None or qty != int(qty):
        return block('invalid_entry_basis_or_quantity')
    eps = basis / (100 * qty)
    spread = (journal or {}).get('spread') or {}
    ids = [int(con_id)]
    if spread.get('short_con_id'):
        ids.append(int(spread['short_con_id']))
        try:
            long_strike, short_strike = float(journal['strike']), float(spread['short_strike'])
            debit = long_strike > short_strike if journal.get('right') == 'P' else long_strike < short_strike
            if not debit:
                return block('unsupported_credit_structure')
        except (KeyError, TypeError, ValueError):
            return block('unknown_spread_structure')
    rows, times, ticks = [], [], []
    now_utc = now_utc or datetime.now(timezone.utc)
    receipt_times = []
    for cid in ids:
        q = (quotes or {}).get(cid) or {}
        if isinstance(q.get('market_data_type'), bool) or q.get('market_data_type') != 1:
            return block(f'nonlive_or_unknown_market_data:{cid}')
        bid, ask = _positive(q.get('bid')), _positive(q.get('ask'))
        if bid is None or ask is None or bid > ask:
            return block(f'missing_or_crossed_bid_ask:{cid}')
        try:
            observed = float(q['observed_monotonic'])
            age = float(now_monotonic) - observed
            if not math.isfinite(age) or not 0 <= age <= max_quote_age_s:
                return block(f'stale_quote:{cid}')
        except (KeyError, TypeError, ValueError):
            return block(f'unobserved_quote:{cid}')
        for side in ('bid', 'ask'):
            try:
                receipt = datetime.fromisoformat(str(q[side + '_observed_utc']).replace('Z', '+00:00'))
                if receipt.tzinfo is None:
                    return block(f'unzoned_{side}_receipt:{cid}')
                receipt_age = (now_utc - receipt).total_seconds()
                if not math.isfinite(receipt_age) or not 0 <= receipt_age <= max_quote_age_s:
                    return block(f'stale_{side}_receipt:{cid}')
                receipt_times.append(receipt.timestamp())
            except (KeyError, TypeError, ValueError):
                return block(f'missing_{side}_receipt:{cid}')
        tick = _positive(q.get('min_tick'))
        if tick is None:
            return block(f'unknown_tick_size:{cid}')
        times.append(observed); ticks.append(tick); rows.append((bid, ask, q))
    if max(receipt_times) - min(receipt_times) > max_quote_skew_s:
        return block('asynchronous_spread_quotes')
    executable = rows[0][0] - (rows[1][1] if len(rows) == 2 else 0)
    if executable <= 0:
        return block('nonpositive_executable_net')
    width = sum(ask - bid for bid, ask, _ in rows)

    tick = sum(ticks)
    known_entry_fee = 0.
    fee = (journal or {}).get('entry_commission')
    try:
        fee = float(fee)
        if math.isfinite(fee) and fee >= 0:
            journal_qty = _positive((journal or {}).get('quantity'))
            if journal_qty is not None and journal_qty == int(journal_qty) and journal_qty >= qty:
                known_entry_fee = fee * qty / journal_qty
                out['entry_commission_known'] = True
                out['entry_commission_allocation'] = 'proportional_to_remaining_quantity'
            elif journal_qty is None:


                known_entry_fee = fee
                out['entry_commission_known'] = True
                out['entry_commission_allocation'] = 'unprorated_total_quantity_unknown'
            else:
                out['entry_commission_allocation'] = 'unknown_additional_lot_fees'
    except (TypeError, ValueError):
        pass
    fee_per_share = known_entry_fee / (100 * qty)
    executable_gain = (executable / eps - 1) * 100
    gain_after_known_fees = (executable - eps - fee_per_share) / eps * 100
    out.update(quote_qualified=True, executable_price=executable,
               executable_gain_pct=executable_gain,
               gain_after_known_entry_fees_pct=gain_after_known_fees,
               known_entry_commission=known_entry_fee if out['entry_commission_known'] else None,
               quote_spread=width, observed_tick=tick,
               quote_max_age_s=max(float(now_monotonic)-t for t in times),
               quote_skew_s=max(receipt_times)-min(receipt_times))
    if not atr_record:
        return block('atr_unavailable')
    try:
        asof = date.fromisoformat(str(atr_record['asof'])[:10])
        age_days = (today - asof).days
        if not 0 <= age_days <= max_atr_age_days:
            return block('atr_stale_or_future')
        atr = _positive(atr_record['atr'])
        if atr is None:
            return block('invalid_atr')
        delta = [float(q['delta']) for _, _, q in rows]
        if not all(math.isfinite(d) and abs(d) <= 1 for d in delta):
            return block('invalid_quote_delta')
        net_delta = abs(delta[0] - (delta[1] if len(delta) == 2 else 0))
        if net_delta <= 0 or net_delta > 1:
            return block('invalid_net_delta')
        raw = float(k_arm) * atr * net_delta / eps * 100
        if not math.isfinite(raw) or raw <= 0:
            return block('invalid_atr_activation')
    except (KeyError, TypeError, ValueError):
        return block('atr_or_live_delta_unavailable')



    try:
        giveback = float(giveback_fraction)
        if not math.isfinite(giveback) or not 0 <= giveback < 1:
            return block('invalid_giveback_fraction')
    except (TypeError, ValueError):
        return block('invalid_giveback_fraction')
    retained_cost = tick + fee_per_share
    required_gain = max(width * .5, retained_cost / (1 - giveback))
    noise_cost_pct = required_gain / eps * 100
    activation = max(min(float(ceiling_pct), raw), noise_cost_pct)
    out.update(activation_gain_pct=activation, atr=atr, atr_asof=asof.isoformat(),
               atr_age_days=age_days, net_delta=net_delta, entry_per_share=eps,
               per_atr_pct=atr*net_delta/eps*100,
               noise_and_known_cost_floor_pct=noise_cost_pct,
               qualification_giveback_fraction=giveback,
               minimum_retained_known_cost_per_share=retained_cost,
               retained_gain_at_activation_per_share=eps*activation/100*(1-giveback),
               configured_ceiling_pct=float(ceiling_pct),
               friction_above_ceiling=noise_cost_pct > float(ceiling_pct))
    if executable_gain < activation:
        return block('current_executable_gain_below_activation')
    out.update(qualified=True, reason='qualified_fresh_executable_gain')
    return out
