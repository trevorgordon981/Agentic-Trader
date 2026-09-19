"""Public API contract; production-derived narrative omitted."""

import ast
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]




SKIP_DIRS = {"__pycache__", ".git", "tests", "data", "logs", "venv", ".venv", "node_modules",
             "site-packages", "htmlcov", ".pytest_cache"}

NUMERIC_WORDS = (
    "qty", "quantity", "size", "count", "num", "n_", "volume",
    "id", "conid", "con_id", "permid", "orderid", "contract_id",
    "price", "px", "debit", "credit", "cost", "basis", "amount", "notional", "proceeds",
    "pnl", "pct", "percent", "ratio", "fraction", "multiple", "factor", "weight",
    "days", "dte", "minutes", "seconds", "hours", "ttl", "cycles", "interval", "timeout",
    "delta", "gamma", "theta", "vega", "iv", "strike", "width", "mark", "limit", "bid", "ask",
    "threshold", "floor", "ceiling", "cap", "max", "min", "target", "level", "halflife",
    "score", "conviction", "stop", "trail", "spread_pct", "value", "balance", "equity",
)


def _read_name(node):
    """Public API contract; production-derived narrative omitted."""
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in ("get", "pop"):
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                return node.args[0].value.lower()
            return None
        if isinstance(f, ast.Name) and f.id == "getattr":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                    and isinstance(node.args[1].value, str):
                return node.args[1].value.lower()
            return None
        if isinstance(f, ast.Name) and f.id in ("int", "float"):
            return _read_name(node.args[0]) if node.args else None
        return None
    if isinstance(node, ast.Attribute):
        return node.attr.lower()
    if isinstance(node, ast.Subscript):
        s = node.slice
        if isinstance(s, ast.Constant) and isinstance(s.value, str):
            return s.value.lower()
        return None
    if isinstance(node, ast.Name):
        return node.id.lower()
    return None


def _numericish(name):
    return name is not None and any(w in name for w in NUMERIC_WORDS)


class _OrDefaultVisitor(ast.NodeVisitor):
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, relpath, src):
        self.relpath, self.src, self.hits, self.scope, self.bool_ctx = relpath, src, [], [], set()

    def _mark_bool(self, node):
        if node is not None:
            self.bool_ctx.add(id(node))

    def visit_If(self, n):
        self._mark_bool(n.test)
        self.generic_visit(n)

    def visit_While(self, n):
        self._mark_bool(n.test)
        self.generic_visit(n)

    def visit_Assert(self, n):
        self._mark_bool(n.test)
        self.generic_visit(n)

    def visit_IfExp(self, n):
        self._mark_bool(n.test)
        self.generic_visit(n)

    def visit_UnaryOp(self, n):
        if isinstance(n.op, ast.Not):
            self._mark_bool(n.operand)
        self.generic_visit(n)

    def visit_comprehension(self, n):
        for cond in n.ifs:
            self._mark_bool(cond)
        self.generic_visit(n)

    def visit_FunctionDef(self, n):
        self.scope.append(n.name)
        self.generic_visit(n)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, n):
        self.scope.append(n.name)
        self.generic_visit(n)
        self.scope.pop()

    def generic_visit(self, n):
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
            if id(n) in self.bool_ctx:
                for v in n.values:
                    self._mark_bool(v)
            else:
                self._classify(n)
        super().generic_visit(n)

    def _classify(self, n):
        for left, right in zip(n.values, n.values[1:]):
            lname = _read_name(left)
            if lname is None:
                continue
            hit = False
            if isinstance(right, ast.Constant) and isinstance(right.value, (int, float)) \
                    and not isinstance(right.value, bool) and right.value != 0:
                hit = True
            else:
                rname = _read_name(right)
                if rname is not None and rname != lname and (_numericish(lname) or _numericish(rname)):
                    hit = True
            if hit:
                self.hits.append((
                    self.relpath,
                    ".".join(self.scope) or "<module>",
                    " ".join((ast.get_source_segment(self.src, n) or "").split()),
                ))
                return


def _scan():
    found = set()
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            path = Path(dirpath) / fn
            rel = str(path.relative_to(REPO))
            try:
                src = path.read_text()
                tree = ast.parse(src)
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            v = _OrDefaultVisitor(rel, src)
            v.visit(tree)
            found.update(v.hits)
    return found
















BASELINE = {

    ("exitmgr/manager.py", "ExitManager._credit_received_usd", "contracts or 1"):
        "DIVIDE-GUARD: per-contract credit denominator.",
    ("exitmgr/manager.py", "ExitManager._log_exit", 'je.get("quantity", 1) or 1'):
        "DIVIDE-GUARD: per-contract basis denominator.",
    ("exitmgr/manager.py", "ExitManager._log_exit", 'je.get("quantity", qty) or qty'):
        "DIVIDE-GUARD: _full_q is used only as `comm * qty / _full_q`, itself behind `if _full_q`.",
    ("exitmgr/manager.py", "ExitManager._emit_tool_close", 'je.get("quantity", 1) or 1'):
        "DIVIDE-GUARD: per-contract basis denominator.",
    ("exitmgr/manager.py", "ExitManager._emit_expiry_close", 'je.get("quantity", 1) or 1'):
        "DIVIDE-GUARD: per-contract basis denominator.",
    ("exitmgr/manager.py", "ExitManager._emit_expiry_close", "qty or 1"):
        "DIVIDE-GUARD: per-contract basis denominator.",
    ("exitmgr/manager.py", "ExitManager._process_short_assignments", 'je.get("quantity", 1) or 1'):
        "DIVIDE-GUARD: per-contract basis denominator.",
    ("exitmgr/manager.py", "ExitManager._atr_levels_for", "dte or 1"):
        "DIVIDE-GUARD: sqrt-of-time scaling denominator; a 0-DTE ATR band is undefined.",
    ("exitmgr/manager.py", "ExitManager.run_cycle", "dte or 1"):
        "DIVIDE-GUARD: sqrt-of-time scaling denominator.",
    ("exitmgr/reload_queue.py", "make_ticket", "ttl_cycles or 1"):
        "DIVIDE-GUARD: TTL horizon multiplier; a 0-cycle TTL expires a ticket before it exists.",
    ("exitmgr/reload_queue.py", "make_ticket", "interval_seconds or 1"):
        "DIVIDE-GUARD: cadence multiplier; a 0s loop interval is not a schedule.",
    ("exitmgr/reload_queue.py", "reload_friction_ok", "qty or 1"):
        "DIVIDE-GUARD: per-contract friction denominator.",
    ("exitmgr/trader.py", "Trader._entry_record", 'getattr(r, "dte", 0) or 1'):
        "DIVIDE-GUARD: annualisation denominator on the journal row.",
    ("daily_recommend.py", "run", 'getattr(fresh_r, "dte", 1) or 1'):
        "DIVIDE-GUARD (not owned by this change): annualisation denominator.",


    ("exitmgr/manager.py", "ExitManager._existing_exit_for_order", "identity_match or legacy_match"):
        "BOOLEAN: two predicates. Detector matched on the substring 'id' in 'identity_match'.",
    ("exitmgr/manager.py", "ExitManager._dataset_has_exit_order", "identity_match or legacy_match"):
        "BOOLEAN: two predicates.",
    ("exitmgr/manager.py", "ExitManager._emit_tool_close", "long_marker or short_marker or {}"):
        "BOOLEAN: picks whichever marker dict exists. Matched on 'mark' inside 'marker'.",
    ("exitmgr/manager.py", "ExitManager.run_cycle", "caps_ok or _protective"):
        "BOOLEAN: two predicates. Matched on 'cap' inside 'caps_ok'.",
    ("daily_recommend.py", "run", "ovr or full_size or qty_ovr or ov_tp or ov_sl"):
        "BOOLEAN: 'did the approver send ANY override', not an arithmetic default.",
    ("dd_consider.py", "main", "args.force_stage2 or args.assume_market_open or args.fixture"):
        "BOOLEAN: three CLI flags.",


    ("exitmgr/event_capture.py", "store_dir", 'os.environ.get("TRADE_CAPTURE_DIR") or _DEFAULT_DIR'):
        "TEXT: an empty env var IS unset for a path.",
    ("exitmgr/manager.py", "ExitManager.assess_positions_offcycle",
     '(meta or {}).get("model_identity") or getattr(self.config, "llm_model", None)'):
        "TEXT: model identity strings.",
    ("exitmgr/approval.py", "model_display_name",
     'runtime.get("model_id") or identity.get("model_id") or identity.get("model_realpath") or runtime.get("model_realpath") or identity.get("artifact_id") or "Unknown model"'):
        "TEXT: ordered runtime-identity labels; empty and absent both mean no usable label.",
    ("exitmgr/setup_watchlist.py", "_clean_field", "value or default"):
        "TEXT: Markdown field cleanup; an empty field deliberately uses the text default.",
    ("backfill_from_exits_log.py", "main", "args.decision_dir or args.dataset_dir"):
        "TEXT: an empty directory argument IS unset.",
    ("dd_consider.py", "item_key", 'item.get("capture_id") or item.get("slack_ts")'):
        "TEXT: both are opaque string keys, never numeric.",


    ("exitmgr/manager.py", "ExitManager.run_cycle",
     'je.get("entry_fill_debit") or je.get("debit")'):
        "REVIEWED: a 0.0 entry_fill_debit is impossible for a long debit position, and USING it "
        "as the basis makes realized_pct undefined -- the phantom -100% class this file already "
        "guards against at the avgFillPrice check. Falling back to the journal debit is a "
        "deliberate repair of a corrupt field, not an accidental promotion.",





    ("exitmgr/trader.py", "Trader._resolve_credit_order", "_bounds.max_dte or CREDIT_MAX_DTE_DEFAULT"):
        "REVIEWED: a max_dte of 0 sits below the min_dte floor, so it selects no expiry at all. "
        "It is arguably a credit-side kill switch this idiom eats -- flagged in the change "
        "report rather than altered, because changing credit tenor selection is a trading "
        "decision, not a cleanup.",
    ("exitmgr/trader.py", "Trader._resolve_order", 'atm_iv or enrich.get("entry_iv") or 0.0'):
        "REVIEWED: a 0.0 implied vol is not a quote, and the terminal default is 0.0 anyway, so "
        "no non-zero value is ever invented here.",
    ("SYMZ_put_chain.py", "main", "bid or ask"):
        "REVIEWED: a 0 bid falls to the ask in a read-only chain-printing script. Real, but it "
        "prints a number for a human rather than deciding anything.",


    ("exitmgr/construction.py", "open_book_items",
     'getattr(order, "permId", None) or getattr(order, "orderId", None) or ref'):
        "REPORTED, not owned: permId 0 falls through to orderId -- the id-chain form of this "
        "class, in the open-book dedupe key.",
    ("exitmgr/construction.py", "open_expiry_counts",
     'getattr(order, "permId", None) or getattr(order, "orderId", None) or ref'):
        "REPORTED, not owned: same id chain, in the per-expiry concentration count.",
    ("exitmgr/construction.py", "earnings_ok",
     'getattr(cons, "earnings_hold_slip_mult", 2.0) or 2.0'):
        "REPORTED, not owned: `earnings_hold_slip_mult: 0` (disable the slip) reads as 2.0 -- "
        "exactly the kill-switch-that-cannot-kill shape manager._cfg_num was written for.",
    ("exitmgr/order.py", "OrderManager._normalize_short_context", 'out.get("position_qty") or q'):
        "REPORTED, not owned: a recorded position_qty of 0 falls through to the broker quantity, "
        "the short-side twin of the manager close_qty defect fixed by this change.",
    ("exitmgr/state.py", "reconcile_state", "live_order_id or in_flight.order_id or 0"):
        "REPORTED, not owned: IB orderId 0 is InFlightClose's documented 'not yet placed' "
        "sentinel, so 0 is genuinely absent here -- but the chain is the same shape and is "
        "listed so a future reader does not have to re-derive that.",
    ("exitmgr/flex_ingest.py", "_aggregate_pair",
     'row["entry"].get("debit") or row["entry"].get("credit")'):
        "REPORTED, not owned: a 0.0 debit reads the CREDIT field instead, which flips a long "
        "position's sign convention in the Flex reconciliation.",
}


def test_no_new_falsy_zero_defaults():
    found = _scan()
    new = sorted(found - set(BASELINE))
    assert not new, (
        "\n\nNEW `X or Y` DEFAULT(S) THAT WOULD EAT A LEGITIMATE ZERO:\n\n"
        + "\n".join("  %s  [%s]\n      %s" % (f, fn, expr) for f, fn, expr in new)
        + "\n\n`0 or default` is `default` in Python. This shape has caused at least five separate\n"
          "defects in this repository, including a zero-quantity close instruction executing as a\n"
          "full-position exit. Do ONE of:\n\n"
          "  (a) If a zero is a legitimate value for that field, stop using `or`. Test for\n"
          "      absence explicitly -- `x if x is not None else default`, or the helpers\n"
          "      exitmgr.manager._first_present / _cfg_num / _durable_qty.\n\n"
          "  (b) If a zero is genuinely invalid there (a denominator, a flag, a string), add the\n"
          "      entry to BASELINE in tests/test_falsy_zero_defaults.py WITH THE REASON. Writing\n"
          "      the reason down is the point: the ones with no good reason are the bugs.\n")


def test_the_baseline_does_not_rot():
    """Public API contract; production-derived narrative omitted."""
    found = _scan()
    stale = sorted(set(BASELINE) - found)
    assert not stale, (
        "\n\nBASELINE entries no longer present in the source (good -- but delete them):\n\n"
        + "\n".join("  %s  [%s]\n      %s" % (f, fn, expr) for f, fn, expr in stale) + "\n")


def test_the_detector_actually_detects_the_defects_it_is_named_for():
    """Public API contract; production-derived narrative omitted."""
    import textwrap

    def hits(code):
        src = textwrap.dedent(code)
        v = _OrDefaultVisitor("probe.py", src)
        v.visit(ast.parse(src))
        return [h[2] for h in v.hits]


    assert hits('planned_qty = int(ctx.get("close_qty") or inf.remaining_qty or 0)')
    assert hits('cid = r.get("con_id") or r.get("contract_id")')
    assert hits('mins = float(getattr(cons, "fill_alarm_minutes", 15) or 15)')
    assert hits('_qf = trigger.quantity_fraction or 1.0')
    assert hits('limit = getattr(_CFG, "value", None) or max_spread_pct')


    assert not hits('if a.get("x") or b.get("qty"):\n    pass')
    assert not hits('if not (a.get("qty") or b.get("qty")):\n    pass')
    assert not hits('ys = [z for z in xs if z.get("qty") or z.get("size")]')
    assert not hits('while cfg.qty or cfg.size:\n    pass')


    assert not hits('n = rec.get("qty") or 0')
    assert not hits('s = rec.get("symbol") or ""')
    assert not hits('d = rec.get("extra") or {}')


    assert not hits('q = _durable_qty(ctx, "close_qty", inf.remaining_qty)')
    assert not hits('cid = _first_present(r, "con_id", "contract_id")')
    assert not hits('m = _cfg_num(cons, "fill_alarm_minutes", 15)')
    assert not hits('v = x if x is not None else default')


def test_the_scan_reaches_the_files_this_change_touched():
    """Public API contract; production-derived narrative omitted."""
    files = {f for f, _, _ in BASELINE}
    assert "exitmgr/manager.py" in files and "exitmgr/trader.py" in files
    for rel in files:
        assert (REPO / rel).exists(), rel
    assert len(_scan()) >= len(BASELINE)
