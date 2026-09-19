"""Public API contract; production-derived narrative omitted."""
import datetime
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flex_reconcile as fr



SYMA_TRUE, SYMA_WRONG = -600.0, -900.0
SYMB_TRUE, SYMB_WRONG = -100.0, -500.0


def _positions_ok():
    return {"blocking": False, "status": "ok", "lines": [], "unmanaged": (),
            "quantity_mismatches": (), "phantom": ()}


def _stmt(tmp_path, name, from_d, to_d, trades):
    """Public API contract; production-derived narrative omitted."""
    legs = "\n".join(
        '      <Trade underlyingSymbol="%s" symbol="%s" fifoPnlRealized="%s"/>' % (u, u, p)
        for u, p in trades)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<FlexQueryResponse>\n <FlexStatements count="1">\n'
           '  <FlexStatement accountId="U1" fromDate="%s" toDate="%s">\n'
           '   <Trades>\n%s\n   </Trades>\n'
           '  </FlexStatement>\n </FlexStatements>\n</FlexQueryResponse>\n'
           % (from_d, to_d, legs))
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return str(p)


def _exits(tmp_path, rows, name="exits.log"):
    """Public API contract; production-derived narrative omitted."""
    p = tmp_path / name
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return str(p)


def _row(symbol, close_ts, realized, **kw):
    r = {"symbol": symbol, "close_ts": close_ts, "realized_pnl": realized}
    r.update(kw)
    return r



def test_the_statement_is_chosen_by_its_window_not_its_filename(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    arch = tmp_path / "archive"
    arch.mkdir()
    good = _stmt(arch, "flex-statement-20370822T011619.xml", "20310821", "20370820", [("SYMA", -285.73)])
    for i in range(5):
        _stmt(arch, "flex-statement-2037082%dT110000.xml" % (3 + i), "20310703", "20370702", [])
    picked = fr.newest_statement(archive=str(arch))
    assert picked == good, (
        "picked %s -- filename order, not window order" % os.path.basename(picked or ""))
    assert fr.statement_window(picked) == ("2031-08-21", "2037-08-20")


def test_identical_windows_fall_back_to_the_newest_filename(tmp_path):
    arch = tmp_path / "archive"
    arch.mkdir()
    _stmt(arch, "flex-statement-20370820T010000.xml", "20310821", "20370820", [])
    newer = _stmt(arch, "flex-statement-20370822T010000.xml", "20310821", "20370820", [])
    assert fr.newest_statement(archive=str(arch)) == newer


def test_an_empty_archive_is_not_a_crash(tmp_path):
    arch = tmp_path / "archive"
    arch.mkdir()
    assert fr.newest_statement(archive=str(arch)) is None


def test_the_window_is_read_from_the_head_without_a_full_parse(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    p = _stmt(tmp_path, "flex-statement-20370822T010000.xml", "20310703", "20370702", [])
    with open(p, "a") as fh:
        fh.write("\n<!-- " + "x" * 200000 + " -->\n")
    assert fr.statement_window(p) == ("2031-07-03", "2037-07-02")



def test_rows_past_the_window_end_are_reported_not_swallowed(tmp_path):
    stmt = _stmt(tmp_path, "s.xml", "20310703", "20370702", [("SYMA", SYMA_TRUE)])
    ex = _exits(tmp_path, [_row("SYMA", "2037-08-20T06:30:31-07:00", SYMA_WRONG)])
    rows, uncovered, window, _ = fr.compare(stmt, exits_path=ex)
    assert rows == []
    assert [(u[0], u[3]) for u in uncovered] == [("SYMA", "closed after the statement window")]
    assert window == ("2031-07-03", "2037-07-02")


def test_rows_before_the_window_start_are_reported_too(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    stmt = _stmt(tmp_path, "s.xml", "20370801", "20370821", [("SYMB", SYMB_TRUE)])
    ex = _exits(tmp_path, [_row("SYMB", "2037-06-29T10:00:00-07:00", -100.0)])
    rows, uncovered, _, _ = fr.compare(stmt, exits_path=ex)
    assert rows == [], "an out-of-window row must not be compared against a total that excludes it"
    assert [(u[0], u[3]) for u in uncovered] == [("SYMB", "closed before the statement window")]


def test_the_verdict_is_not_green_while_anything_is_unexamined(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    stmt = _stmt(tmp_path, "s.xml", "20310703", "20370702", [("IWM", 10.0)])
    ex = _exits(tmp_path, [
        _row("IWM", "2037-06-30T10:00:00-07:00", 10.0),
        _row("SYMA", "2037-08-20T06:30:31-07:00", SYMA_WRONG),
        _row("SYMB", "2037-08-17T12:51:26-07:00", SYMB_WRONG),
    ])
    rows, uncovered, window, _ = fr.compare(stmt, exits_path=ex)
    text = fr.render(rows, uncovered, window, fr.TOLERANCE, positions=_positions_ok())
    assert rows == []
    assert len(uncovered) == 2
    assert "white_check_mark" not in text
    assert "were NOT checked" in text
    for sym in ("SYMA", "SYMB"):
        assert sym in text


def test_green_is_still_reachable_when_coverage_is_complete(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    stmt = _stmt(tmp_path, "s.xml", "20310821", "20370821",
                 [("SYMA", SYMA_TRUE), ("SYMB", SYMB_TRUE)])
    ex = _exits(tmp_path, [_row("SYMA", "2037-08-20T06:30:31-07:00", SYMA_TRUE),
                           _row("SYMB", "2037-08-17T12:51:26-07:00", SYMB_TRUE)])
    rows, uncovered, window, _ = fr.compare(stmt, exits_path=ex)
    assert (rows, uncovered) == ([], [])
    text = fr.render(rows, uncovered, window, fr.TOLERANCE, positions=_positions_ok())
    assert "white_check_mark" in text and "every closed row we hold is inside the window" in text


def test_small_symbol_residuals_cannot_hide_an_aggregate_money_mismatch(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    stmt = _stmt(tmp_path, "s.xml", "20310821", "20370821",
                 [("AAA", -14.0), ("BBB", -14.0)])
    ex = _exits(tmp_path, [
        _row("AAA", "2037-08-20T10:00:00-07:00", -1.0),
        _row("BBB", "2037-08-20T10:01:00-07:00", -1.0),
    ])
    rows, uncovered, window, (broker, local) = fr.compare(
        stmt, tolerance=25.0, exits_path=ex)
    assert rows == [] and uncovered == []
    aggregate = fr.aggregate_reconciliation(broker, local, tolerance=25.0)
    assert aggregate["residual"] == pytest.approx(-26.0)
    assert aggregate["within_tolerance"] is False
    text = fr.render(rows, uncovered, window, 25.0, aggregate=aggregate,
                     positions=_positions_ok())
    assert "white_check_mark" not in text
    assert "aggregate money mismatch" in text and "$-26.00 residual" in text


def test_main_exits_nonzero_on_aggregate_mismatch(tmp_path, monkeypatch):
    stmt = _stmt(tmp_path, "s.xml", "20310821", "20370821",
                 [("AAA", -14.0), ("BBB", -14.0)])
    _exits(tmp_path, [
        _row("AAA", "2037-08-20T10:00:00-07:00", -1.0),
        _row("BBB", "2037-08-20T10:01:00-07:00", -1.0),
    ])
    monkeypatch.setattr(fr, "APP", str(tmp_path))
    assert fr.main(["--statement", stmt, "--dry-run", "--tolerance", "25"]) == 1


def test_an_old_window_with_nothing_after_it_is_honest_coverage(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    stmt = _stmt(tmp_path, "s.xml", "20310703", "20370702", [("IWM", 10.0)])
    ex = _exits(tmp_path, [_row("IWM", "2037-06-30T10:00:00-07:00", 10.0)])
    rows, uncovered, window, _ = fr.compare(stmt, exits_path=ex)
    assert (rows, uncovered) == ([], [])
    assert "white_check_mark" in fr.render(rows, uncovered, window, fr.TOLERANCE,
                                             positions=_positions_ok())


def test_no_comparable_money_is_unevaluated_not_green():
    aggregate = fr.aggregate_reconciliation({"BROKER": 1.0}, {"LOCAL": 1.0})
    assert aggregate["matched_underlyings"] == 0
    assert aggregate["within_tolerance"] is None
    text = fr.render([], [], ("2037-01-01", "2037-12-31"), fr.TOLERANCE,
                     aggregate=aggregate, positions=_positions_ok())
    assert "white_check_mark" not in text and "UNEVALUATED" in text


def test_the_real_divergence_is_caught_once_the_window_reaches_it(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    stmt = _stmt(tmp_path, "s.xml", "20310821", "20370821",
                 [("SYMA", SYMA_TRUE), ("SYMB", SYMB_TRUE)])
    ex = _exits(tmp_path, [_row("SYMA", "2037-08-20T06:30:31-07:00", SYMA_WRONG),
                           _row("SYMB", "2037-08-17T12:51:26-07:00", SYMB_WRONG)])
    rows, uncovered, _, _ = fr.compare(stmt, exits_path=ex)
    assert uncovered == []
    got = {u: round(d, 2) for u, _b, _m, d, _w in rows}
    assert got == {"SYMA": 300.0, "SYMB": 400.0}


def test_main_exits_nonzero_when_coverage_is_incomplete(tmp_path, monkeypatch):
    stmt = _stmt(tmp_path, "s.xml", "20310703", "20370702", [])
    ex = _exits(tmp_path, [_row("SYMA", "2037-08-20T06:30:31-07:00", SYMA_WRONG)])
    monkeypatch.setattr(fr, "APP", str(tmp_path))
    monkeypatch.setattr(fr, "post", lambda text: pytest.fail("must not post during a test"))
    assert fr.main(["--statement", stmt, "--dry-run"]) == 1



def _position_statement(tmp_path, rows, generated="20370823;220000"):
    body = "\n".join(
        '<OpenPosition conid="%s" position="%s" underlyingSymbol="%s" '
        'putCall="%s" strike="%s" positionValue="100" />'
        % (cid, qty, symbol, right, strike)
        for cid, qty, symbol, right, strike in rows)
    path = tmp_path / "flex-positions-test.xml"
    path.write_text(
        '<FlexQueryResponse><FlexStatements><FlexStatement whenGenerated="%s">'
        '<OpenPositions>%s</OpenPositions></FlexStatement></FlexStatements></FlexQueryResponse>'
        % (generated, body))
    return str(path)


def _position_now():
    return datetime.datetime(2037, 8, 23, 22, 30, tzinfo=datetime.timezone.utc)


def test_unmanaged_broker_position_is_blocking_not_footer_only(tmp_path, monkeypatch):
    stmt = _position_statement(tmp_path, [(101, 1, "AAA", "C", 10)])
    _exits(tmp_path, [])
    _exits(tmp_path, [], name="trades.log")
    monkeypatch.setattr(fr, "APP", str(tmp_path))
    result = fr.position_coverage_result(stmt, now=_position_now())
    assert result["blocking"] is True and result["unmanaged"] == (101,)
    text = fr.render([], [], ("2037-01-01", "2037-12-31"), fr.TOLERANCE,
                     aggregate={"matched_underlyings": 1, "broker_total": 0,
                                "local_total": 0, "residual": 0,
                                "comparable": True, "within_tolerance": True},
                     positions=result)
    assert "white_check_mark" not in text
    assert "journal does not manage" in text


def test_position_quantity_and_sign_mismatch_is_blocking(tmp_path, monkeypatch):
    stmt = _position_statement(tmp_path, [(101, -1, "AAA", "C", 10)])
    _exits(tmp_path, [])
    _exits(tmp_path, [{"contract_id": 101, "symbol": "AAA", "quantity": 1}],
           name="trades.log")
    monkeypatch.setattr(fr, "APP", str(tmp_path))
    result = fr.position_coverage_result(stmt, now=_position_now())
    assert result["blocking"] is True
    assert result["quantity_mismatches"] == ((101, -1.0, 1.0),)


def test_matching_spread_quantities_and_signs_pass(tmp_path, monkeypatch):
    stmt = _position_statement(tmp_path, [
        (101, 2, "AAA", "C", 10), (102, -2, "AAA", "C", 15)])
    _exits(tmp_path, [])
    _exits(tmp_path, [{"contract_id": 101, "symbol": "AAA", "quantity": 2,
                       "spread": {"short_con_id": 102}}], name="trades.log")
    monkeypatch.setattr(fr, "APP", str(tmp_path))
    result = fr.position_coverage_result(stmt, now=_position_now())
    assert result["blocking"] is False
    assert result["quantity_mismatches"] == () and result["unmanaged"] == ()


def test_missing_corrupt_or_stale_position_evidence_is_blocking(tmp_path, monkeypatch):
    _exits(tmp_path, [])
    _exits(tmp_path, [], name="trades.log")
    monkeypatch.setattr(fr, "APP", str(tmp_path))
    stale = _position_statement(tmp_path, [], generated="20370820;000000")
    assert fr.position_coverage_result(stale, now=_position_now())["blocking"] is True
    (tmp_path / "trades.log").write_text('{"partial":')
    fresh = _position_statement(tmp_path, [])
    result = fr.position_coverage_result(fresh, now=_position_now())
    assert result["blocking"] is True and "not complete JSON" in result["lines"][0]


def test_main_returns_evidence_error_on_malformed_exits(tmp_path, monkeypatch):
    stmt = _stmt(tmp_path, "s.xml", "20310821", "20370821", [("AAA", 1.0)])
    (tmp_path / "exits.log").write_text('{"partial":')
    monkeypatch.setattr(fr, "APP", str(tmp_path))
    assert fr.main(["--statement", stmt, "--dry-run"]) == 2



def _repairable(tmp_path):
    stmt = _stmt(tmp_path, "flex-statement-20370822T011619.xml", "20310821", "20370821",
                 [("SYMA", SYMA_TRUE), ("SYMB", SYMB_TRUE)])
    ex = _exits(tmp_path, [
        _row("SYMA", "2037-08-20T06:30:31-07:00", SYMA_WRONG, close_qty=6, entry_fill_debit=900.0,
             avg_fill_price=0.0, limit_price=0.75004695, trigger_mark=0.75004695,
             close_identity="perm:3001002:con:3001001", perm_id=3001002),
        _row("SYMB", "2037-08-17T12:51:26-07:00", SYMB_WRONG, close_qty=1, entry_fill_debit=500.0,
             avg_fill_price=0.0, limit_price=2.206050900000001, trigger_mark=2.206050900000001,
             close_identity="perm:3001004:con:3001005", perm_id=3001004),
    ])
    rows, uncovered, _, (bro, _m) = fr.compare(stmt, exits_path=ex)
    return fr.propose_repairs(rows, uncovered, bro, stmt, exits_path=ex), ex


def test_a_proposal_reproduces_the_stored_pnl_to_the_cent(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    doc, _ = _repairable(tmp_path)
    assert doc["refused"] == []
    by = {p["symbol"]: p for p in doc["proposals"]}
    assert set(by) == {"SYMA", "SYMB"}
    assert by["SYMA"]["proposed"]["avg_fill_price"] == 0.5
    assert by["SYMA"]["proposed"]["realized_pnl"] == SYMA_TRUE
    assert by["SYMB"]["proposed"]["avg_fill_price"] == 4.0
    assert by["SYMB"]["proposed"]["realized_pnl"] == SYMB_TRUE
    for p in doc["proposals"]:
        assert p["broker_realized_exact"] == p["proposed"]["realized_pnl"]
    for p in doc["proposals"]:
        price = p["proposed"]["avg_fill_price"]
        qty = 6 if p["symbol"] == "SYMA" else 1
        debit = 900.0 if p["symbol"] == "SYMA" else 500.0
        assert round(price * 100 * qty - debit, 2) == p["proposed"]["realized_pnl"]


def test_the_proposal_never_derives_anything_from_limit_price_or_trigger_mark(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    baseline, ex = _repairable(tmp_path)
    poisoned = []
    for line in open(ex):
        r = json.loads(line)
        r["limit_price"] = 999.99
        r["trigger_mark"] = -999.99
        poisoned.append(r)
    ex2 = _exits(tmp_path, poisoned, name="exits2.log")
    stmt = os.path.join(str(tmp_path), "flex-statement-20370822T011619.xml")
    rows, unc, _, (bro, _m) = fr.compare(stmt, exits_path=ex2)
    doc = fr.propose_repairs(rows, unc, bro, stmt, exits_path=ex2)
    assert doc["proposals"] == baseline["proposals"], "a limit_price change moved the answer"
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "flex_reconcile.py")).read()
    body = src.split("def propose_repairs", 1)[1].split("\ndef ", 1)[0]
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
    code = code.split('"""', 2)[-1]
    assert "limit_price" not in code and "trigger_mark" not in code


def test_a_repaired_row_stays_distinguishable(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    doc, _ = _repairable(tmp_path)
    p = next(x for x in doc["proposals"] if x["symbol"] == "SYMA")["proposed"]
    assert p["pnl_repair_source"] == p["avg_fill_price_source"] == "ibkr_flex_20370822T011619"
    pre = [k for k in p if k.startswith("realized_pnl_pre_repair_")]
    assert len(pre) == 1 and p[pre[0]] == SYMA_WRONG
    assert any(k.startswith("avg_fill_price_pre_repair_") for k in p)
    assert doc["schema"] == "flex-repair-proposal.v1"


def test_netting_ambiguity_is_refused_not_guessed(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    stmt = _stmt(tmp_path, "s.xml", "20310821", "20370821", [("SYMA", -100.0)])
    ex = _exits(tmp_path, [
        _row("SYMA", "2037-08-20T06:30:31-07:00", -400.0, close_qty=6, entry_fill_debit=900.0),
        _row("SYMA", "2037-08-19T06:30:31-07:00", -50.0, close_qty=1, entry_fill_debit=100.0),
    ])
    rows, unc, _, (bro, _m) = fr.compare(stmt, exits_path=ex)
    doc = fr.propose_repairs(rows, unc, bro, stmt, exits_path=ex)
    assert doc["proposals"] == []
    assert len(doc["refused"]) == 1 and "netted" in doc["refused"][0]["why"]


def test_a_row_the_broker_never_saw_is_refused(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    stmt = _stmt(tmp_path, "s.xml", "20310821", "20370821", [("SYMA", -285.73)])
    ex = _exits(tmp_path, [_row("SYMA", "2037-08-20T06:30:31-07:00", -285.73, close_qty=6,
                                entry_fill_debit=900.0),
                           _row("IWM", "2037-07-11T10:00:00-07:00", -237.05, close_qty=1,
                                entry_fill_debit=237.05)])
    rows, unc, _, (bro, _m) = fr.compare(stmt, exits_path=ex)
    doc = fr.propose_repairs(rows, unc, bro, stmt, exits_path=ex)
    assert doc["proposals"] == []
    assert [r["symbol"] for r in doc["refused"]] == ["IWM"]
    assert "never saw" in doc["refused"][0]["why"]


def test_a_row_without_the_arithmetic_is_refused(tmp_path):
    stmt = _stmt(tmp_path, "s.xml", "20310821", "20370821", [("SYMA", SYMA_TRUE)])
    ex = _exits(tmp_path, [_row("SYMA", "2037-08-20T06:30:31-07:00", SYMA_WRONG)])
    rows, unc, _, (bro, _m) = fr.compare(stmt, exits_path=ex)
    doc = fr.propose_repairs(rows, unc, bro, stmt, exits_path=ex)
    assert doc["proposals"] == []
    assert "close_qty" in doc["refused"][0]["why"]


def test_the_proposal_is_never_written_over_a_live_file(tmp_path):
    doc, _ = _repairable(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    for name in ("exits.log", "trades.log", "exitmgr_state.json", "events.jsonl"):
        with pytest.raises(SystemExit):
            fr.write_proposal(doc, str(target / name))
        assert not (target / name).exists(), "%s was created before the refusal" % name
    out = fr.write_proposal(doc, str(tmp_path / "sub" / "proposal.json"))
    assert json.load(open(out))["apply_to"]


def test_propose_leaves_exits_log_untouched(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    doc, ex = _repairable(tmp_path)
    before = open(ex, "rb").read()
    fr.write_proposal(doc, str(tmp_path / "p.json"))
    assert open(ex, "rb").read() == before


def test_the_pnl_is_rounded_before_the_price_is_derived_from_it(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    exact = -100.0
    stmt = _stmt(tmp_path, "flex-statement-20370822T160008.xml", "20310822", "20370821",
                 [("SYMB", 0), ("SYMB", -550.0), ("SYMB", 0), ("SYMB", 450.0)])
    ex = _exits(tmp_path, [_row("SYMB", "2037-08-17T12:51:26-07:00", SYMB_WRONG,
                                close_qty=1, entry_fill_debit=500.0)])
    rows, unc, _, (bro, _m) = fr.compare(stmt, exits_path=ex)
    assert bro["SYMB"] == pytest.approx(exact, abs=1e-9)
    doc = fr.propose_repairs(rows, unc, bro, stmt, exits_path=ex)
    p = doc["proposals"][0]
    assert p["proposed"]["realized_pnl"] == round(exact, 2) == -100.0
    assert p["proposed"]["avg_fill_price"] == 4.0
    assert p["broker_realized_exact"] == pytest.approx(exact, abs=1e-9)

    assert round(p["proposed"]["avg_fill_price"] * 100 * 1 - 500.0, 2) == p["proposed"]["realized_pnl"]


def test_the_derived_price_is_the_structure_net_not_a_leg_price(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    stmt = _stmt(tmp_path, "s.xml", "20310822", "20370821",
                 [("SYMA", 300.0), ("SYMA", -900.0)])
    ex = _exits(tmp_path, [_row("SYMA", "2037-08-20T06:30:31-07:00", SYMA_WRONG,
                                close_qty=6, entry_fill_debit=900.0)])
    rows, unc, _, (bro, _m) = fr.compare(stmt, exits_path=ex)
    p = fr.propose_repairs(rows, unc, bro, stmt, exits_path=ex)["proposals"][0]
    assert p["proposed"]["avg_fill_price"] == 0.5 != pytest.approx(0.6)
    assert p["proposed"]["realized_pnl"] == SYMA_TRUE


def test_a_pnl_the_stored_price_precision_cannot_express_is_refused(tmp_path):
    """Public API contract; production-derived narrative omitted."""
    stmt = _stmt(tmp_path, "s.xml", "20310822", "20370821", [("SYMA", -599.99)])
    ex = _exits(tmp_path, [_row("SYMA", "2037-08-20T06:30:31-07:00", SYMA_WRONG,
                                close_qty=6, entry_fill_debit=900.0)])
    rows, unc, _, (bro, _m) = fr.compare(stmt, exits_path=ex)
    doc = fr.propose_repairs(rows, unc, bro, stmt, exits_path=ex)
    assert doc["proposals"] == [], "a P&L the stored precision cannot express was proposed anyway"
    assert len(doc["refused"]) == 1
    why = doc["refused"][0]["why"]
    assert "does not reconcile" in why and "-600.0" in why and "-599.99" in why, why
