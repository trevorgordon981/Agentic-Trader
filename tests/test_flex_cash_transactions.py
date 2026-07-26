"""Cash-transaction ledger tests for exitmgr.flex_ingest (2026-07-26).

WHY THESE EXIST
---------------
From 2026-07-26 the IBKR account receives $500/month, so the balance is no longer a performance
signal.  ~/pnl-tracker/pnl_net.py separates trading P&L from cash flows, but unaided it can only
compute a RESIDUAL -- "money that moved and P&L cannot explain" -- and a residual cannot tell a
$500 wire from a $500 dividend.  These tests pin the behaviour that makes the tracker
AUTHORITATIVE rather than merely conservative:

  * a deposit is CAPITAL and is removed from performance;
  * a dividend is INCOME and is NOT a contribution (calling it one understates the strategy);
  * a withdrawal is negative capital;
  * fees / interest / tax are income;
  * re-running never duplicates a row;
  * a Flex query WITHOUT the Cash Transactions section fails LOUDLY and writes nothing --
    an empty ledger would read as "no contributions" and quietly corrupt the report;
  * malformed rows are dropped without taking the rest of the statement with them;
  * and the pre-existing parse_statement({fills, meta}) contract is bit-for-bit unchanged.

Every ledger write in this file is redirected to tmp_path via $EXITMGR_CASH_LEDGER, so no test
can ever touch the operator's real ~/contributions.jsonl.

NEGATIVE CONTROLS.  Several tests are paired with a control that deliberately breaks the thing
under test and asserts the assertion FLIPS.  A test that passes on both the right and the wrong
answer is measuring nothing, and this is the one file where a false green is expensive.
"""
import importlib.util
import json
import os
import sys

import pytest

from exitmgr import flex_ingest as fi

_HERE = os.path.dirname(__file__)
_SAMPLE = os.path.join(_HERE, "flex_sample.xml")
_PNL_NET = os.path.join(os.path.expanduser("~"), "pnl-tracker", "pnl_net.py")

# parse_statement() output over tests/flex_sample.xml, captured from the UNMODIFIED module at
# sha256 71186d1bfa7cf06edfbc3a01e3103a783e50bd4667935abe08a21514135f234b immediately before the
# cash-transaction work landed.  This is the differential: extend, do not alter.
_BASELINE_FILLS = 46
_BASELINE_SHA = "8230a32da7e076fe5b8417279ed5d4230976b48a50aebfa79b30d10fce400f50"
_BASELINE_META = {"accountId": "U00000000", "fromDate": "20250703", "toDate": "20260702",
                  "period": "Last365CalendarDays", "whenGenerated": "20260703;171453"}


# --------------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def ledger_isolation(tmp_path, monkeypatch):
    """No test may write the real ~/contributions.jsonl."""
    p = tmp_path / "contributions.jsonl"
    monkeypatch.setenv(fi.CASH_LEDGER_ENV, str(p))
    return str(p)


_TRADE_ROW = (
    '<Trade accountId="U00000000" currency="USD" fxRateToBase="1" assetCategory="OPT" '
    'symbol="A     260717C00135000" description="A 17JUL26 135 C" conid="871991493" '
    'underlyingSymbol="A" tradeID="9795311075" multiplier="100" strike="135" '
    'reportDate="20260629" expiry="20260717" dateTime="20260629;133649" putCall="C" '
    'quantity="1" tradePrice="2.95" ibCommission="-1.05075" ibCommissionCurrency="USD" '
    'openCloseIndicator="O" fifoPnlRealized="0" buySell="BUY" ibOrderID="5366666180" '
    'ibExecID="0000f84c.6a42aae8.03.01" isAPIOrder="N" levelOfDetail="EXECUTION" />'
)


def _cash(**kw) -> str:
    a = {"accountId": "U00000000", "currency": "USD", "fxRateToBase": "1",
         "levelOfDetail": "DETAIL"}
    a.update(kw)
    return "<CashTransaction " + " ".join(f'{k}="{v}"' for k, v in a.items()) + " />"


def _statement(cash_rows=None, *, with_cash_section=True, trades=True,
               base_currency="USD") -> str:
    """A FlexQueryResponse.  `with_cash_section=False` models the query as it is configured
    TODAY: trades only, no Cash Transactions section at all."""
    trade_block = f"<Trades>{_TRADE_ROW}</Trades>" if trades else ""
    cash_block = ""
    if with_cash_section:
        cash_block = "<CashTransactions>" + "".join(cash_rows or []) + "</CashTransactions>"
    return (
        '<FlexQueryResponse queryName="TradeArchives" type="AF">'
        '<FlexStatements count="1">'
        f'<FlexStatement accountId="U00000000" fromDate="20250725" toDate="20260826" '
        f'period="Last365CalendarDays" whenGenerated="20260826;035650" '
        f'baseCurrency="{base_currency}">'
        f"{trade_block}{cash_block}"
        "</FlexStatement></FlexStatements></FlexQueryResponse>"
    )


DEPOSIT = _cash(type="Deposits/Withdrawals", amount="500", dateTime="20260803;202000",
                settleDate="20260803", reportDate="20260803", transactionID="90000000001",
                description="ELECTRONIC FUND TRANSFER", code="")
DIVIDEND = _cash(type="Dividends", amount="12.50", dateTime="20260814;202000",
                 settleDate="20260814", reportDate="20260814", transactionID="90000000002",
                 symbol="SPY", description="SPY(US78462F1030) CASH DIVIDEND USD 1.25 PER SHARE")
WITHDRAWAL = _cash(type="Deposits/Withdrawals", amount="-250", dateTime="20260820;202000",
                   settleDate="20260820", reportDate="20260820", transactionID="90000000003",
                   description="DISBURSEMENT INITIATED BY Example User")
INTEREST = _cash(type="Broker Interest Received", amount="1.83", dateTime="20260805;202000",
                 settleDate="20260805", reportDate="20260805", transactionID="90000000004",
                 description="USD CREDIT INT FOR JUL-2026")
FEE = _cash(type="Other Fees", amount="-1.25", dateTime="20260806;202000",
            settleDate="20260806", reportDate="20260806", transactionID="90000000005",
            description="BALANCES WITH VALUE AS OF 20260806")
TAX = _cash(type="Withholding Tax", amount="-1.88", dateTime="20260814;202000",
            settleDate="20260814", reportDate="20260814", transactionID="90000000006",
            symbol="SPY", description="SPY(US78462F1030) CASH DIVIDEND - US TAX")
PIL = _cash(type="Payment In Lieu Of Dividends", amount="3.10", dateTime="20260815;202000",
            settleDate="20260815", reportDate="20260815", transactionID="90000000007",
            symbol="T", description="T(US00206R1023) PAYMENT IN LIEU OF DIVIDEND")

ALL_ROWS = [DEPOSIT, DIVIDEND, WITHDRAWAL, INTEREST, FEE, TAX, PIL]


def _rows_by_id(rows):
    return {r.get("id"): r for r in rows}


def _read_ledger(path):
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ============================================================ 1. schema matches pnl_net.py
def test_ledger_schema_matches_pnl_net_vocabulary():
    """pnl_net.py reads exactly four keys -- date / amount / type / note -- and splits `type`
    into CAPITAL_TYPES vs INCOME_TYPES.  Every type this module can emit for a RECOGNISED IBKR
    type must land in one of those two sets, or the tracker silently mislabels it."""
    emitted = set()
    for ibkr_type in fi._CASH_TYPE_MAP:
        for amount in (100.0, -100.0):
            kind, recognised = fi.classify_cash_type(ibkr_type, amount)
            assert recognised is True, ibkr_type
            emitted.add(kind)
    assert emitted <= set(fi.CASH_CAPITAL_TYPES) | set(fi.CASH_INCOME_TYPES)
    # and both halves are actually exercised -- a map that only ever emitted capital would
    # satisfy the subset check above while destroying the entire point of the split.
    assert emitted & set(fi.CASH_CAPITAL_TYPES)
    assert emitted & set(fi.CASH_INCOME_TYPES)


@pytest.mark.skipif(not os.path.exists(_PNL_NET), reason="~/pnl-tracker/pnl_net.py not present")
def test_vocabulary_is_the_real_consumers_vocabulary():
    """Bind to the ACTUAL consumer, not to a copy of it.  If pnl_net.py ever renames a type,
    this fails here rather than silently reclassifying dollars in the report."""
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("_pnl_net_under_test", _PNL_NET)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:                                    # pragma: no cover - env dependent
        pytest.skip(f"pnl_net.py not importable here: {e}")
    finally:
        sys.path[:] = saved
    assert set(fi.CASH_CAPITAL_TYPES) == set(mod.CAPITAL_TYPES)
    assert set(fi.CASH_INCOME_TYPES) == set(mod.INCOME_TYPES)
    assert str(mod.DEFAULT_LEDGER) == fi.DEFAULT_CASH_LEDGER


@pytest.mark.skipif(not os.path.exists(_PNL_NET), reason="~/pnl-tracker/pnl_net.py not present")
def test_emitted_rows_survive_pnl_nets_own_reader(ledger_isolation):
    """End-to-end schema proof: pnl_net.read_jsonl + its own reconcile arithmetic over the
    ledger this module writes, with no adaptation in between."""
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("_pnl_net_reader", _PNL_NET)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:                                    # pragma: no cover - env dependent
        pytest.skip(f"pnl_net.py not importable here: {e}")
    finally:
        sys.path[:] = saved

    fi.ingest_cash_transactions(xml_text=_statement(ALL_ROWS))
    rows = mod.read_jsonl(ledger_isolation)
    assert len(rows) == len(ALL_ROWS)

    cap = inc = 0.0
    for r in rows:
        assert set(("date", "amount", "type")) <= set(r)
        kind = str(r.get("type") or "contribution").strip().lower()
        if kind in mod.INCOME_TYPES:
            inc += float(r["amount"])
        else:
            cap += float(r["amount"])
    # capital = +500 deposit - 250 withdrawal; income = 12.50 + 1.83 - 1.25 - 1.88 + 3.10
    assert round(cap, 2) == 250.00
    assert round(inc, 2) == 14.30


# ================================================================ 2. classification by type
def test_deposit_of_500_is_capital(ledger_isolation):
    s = fi.ingest_cash_transactions(xml_text=_statement([DEPOSIT]))
    assert s["ok"] is True
    row = _read_ledger(ledger_isolation)[0]
    assert row["date"] == "2026-08-03"
    assert row["amount"] == 500.0
    assert row["type"] == "deposit"
    assert row["type"] in fi.CASH_CAPITAL_TYPES
    assert row["type"] not in fi.CASH_INCOME_TYPES
    assert s["capital_total"] == 500.0
    assert s["income_total"] == 0.0
    assert row["ibkr_type"] == "Deposits/Withdrawals"
    assert row["id"] == "90000000001"


def test_dividend_is_income_and_is_not_a_contribution(ledger_isolation):
    """The whole reason this module exists.  A dividend counted as a contribution is removed
    from performance and UNDERSTATES the strategy by exactly its amount."""
    s = fi.ingest_cash_transactions(xml_text=_statement([DIVIDEND]))
    row = _read_ledger(ledger_isolation)[0]
    assert row["type"] == "dividend"
    assert row["type"] in fi.CASH_INCOME_TYPES
    assert row["type"] not in fi.CASH_CAPITAL_TYPES
    assert row["type"] != "contribution"
    assert s["income_total"] == 12.50
    assert s["capital_total"] == 0.0


def test_negative_control_dividend_labelled_as_deposit_would_move_the_dollars():
    """NEGATIVE CONTROL for the test above.  If the classifier were wrong -- if a dividend came
    back as a deposit -- the capital/income split genuinely changes.  Proves the assertions
    above discriminate rather than pass on anything."""
    right, _ = fi.classify_cash_type("Dividends", 12.50)
    wrong, _ = fi.classify_cash_type("Deposits/Withdrawals", 12.50)
    assert right != wrong
    assert (right in fi.CASH_INCOME_TYPES) and (wrong in fi.CASH_CAPITAL_TYPES)


def test_withdrawal_is_negative_capital(ledger_isolation):
    s = fi.ingest_cash_transactions(xml_text=_statement([WITHDRAWAL]))
    row = _read_ledger(ledger_isolation)[0]
    assert row["type"] == "withdrawal"
    assert row["type"] in fi.CASH_CAPITAL_TYPES
    assert row["amount"] == -250.0
    assert s["capital_total"] == -250.0
    assert s["income_total"] == 0.0


def test_deposit_and_withdrawal_share_one_ibkr_type_and_split_on_sign():
    """IBKR reports both directions under the single string 'Deposits/Withdrawals'; the sign is
    the only evidence available, and both halves must stay capital."""
    pos, _ = fi.classify_cash_type("Deposits/Withdrawals", 500.0)
    neg, _ = fi.classify_cash_type("Deposits/Withdrawals", -500.0)
    assert (pos, neg) == ("deposit", "withdrawal")
    assert {pos, neg} <= set(fi.CASH_CAPITAL_TYPES)


def test_fees_interest_tax_and_pil_are_income(ledger_isolation):
    s = fi.ingest_cash_transactions(xml_text=_statement([INTEREST, FEE, TAX, PIL]))
    by_type = {r["type"]: r for r in _read_ledger(ledger_isolation)}
    assert by_type["interest"]["amount"] == 1.83
    assert by_type["fee"]["amount"] == -1.25
    assert by_type["tax"]["amount"] == -1.88
    assert by_type["dividend"]["amount"] == 3.10          # payment in lieu IS a dividend
    assert set(by_type) <= set(fi.CASH_INCOME_TYPES)
    assert s["capital_total"] == 0.0
    assert s["income_total"] == round(1.83 - 1.25 - 1.88 + 3.10, 2)


def test_full_statement_splits_capital_from_income(ledger_isolation):
    s = fi.ingest_cash_transactions(xml_text=_statement(ALL_ROWS))
    assert s["ok"] is True
    assert s["cash_rows"] == 7
    assert s["capital_total"] == 250.00                   # +500 deposit, -250 withdrawal
    assert s["income_total"] == 14.30
    assert s["by_type"] == {"deposit": 500.0, "dividend": 15.60, "fee": -1.25,
                            "interest": 1.83, "tax": -1.88, "withdrawal": -250.0}


def test_unknown_ibkr_type_defaults_to_capital_and_is_reported(ledger_isolation):
    """An IBKR type nobody has mapped must never become profit.  It is emitted with its raw
    slug -- which pnl_net does not find in INCOME_TYPES, so it is held out of performance --
    and it is surfaced so it can be mapped on purpose."""
    weird = _cash(type="Carbon Credit Rebate", amount="42", settleDate="20260901",
                  transactionID="90000000099", description="???")
    s = fi.ingest_cash_transactions(xml_text=_statement([weird]))
    row = _read_ledger(ledger_isolation)[0]
    assert row["type"] not in fi.CASH_INCOME_TYPES        # i.e. pnl_net treats it as capital
    assert row["type_recognised"] is False
    assert "UNMAPPED" in row["note"]
    assert s["unmapped_types"] == [{"ibkr_type": "Carbon Credit Rebate", "rows": 1}]
    assert s["income_total"] == 0.0
    assert s["capital_total"] == 42.0


# ================================================================== 3. missing section = LOUD
def test_missing_cash_section_fails_loudly_and_writes_nothing(ledger_isolation):
    """This is TODAY's live configuration (query 1562555 'TradeArchives' has trades only).
    Emitting an empty ledger here would read as 'no contributions' -- the report would call
    itself authoritative while being blind to every $500 wire."""
    s = fi.ingest_cash_transactions(xml_text=_statement(with_cash_section=False))
    assert s["ok"] is False
    assert s["section_present"] is False
    assert s["ledger_written"] is False
    assert "CashTransactions" in s["note"]
    assert "Cash Transactions" in s["action_required"]
    assert "Flex Quer" in s["action_required"]            # names where to go in the IBKR UI
    assert "Deposits/Withdrawals" in s["action_required"]
    assert "Dividends" in s["action_required"]
    assert not os.path.exists(ledger_isolation)           # NOT an empty file. No file.


def test_negative_control_same_statement_with_the_section_succeeds(ledger_isolation):
    """NEGATIVE CONTROL: the failure above is caused by the MISSING SECTION, not by a broken
    fixture.  Identical XML plus the section writes a real ledger."""
    ok = fi.ingest_cash_transactions(xml_text=_statement([DEPOSIT], with_cash_section=True))
    assert ok["ok"] is True
    assert os.path.exists(ledger_isolation)


def test_missing_section_does_not_break_trade_parsing():
    """The trades in a section-less statement still parse -- so the cash failure is specific."""
    parsed = fi.parse_statement(_statement(with_cash_section=False))
    assert len(parsed["fills"]) == 1


def test_empty_but_present_section_is_ok_and_distinct_from_missing(ledger_isolation):
    """A period with genuinely no cash movement is NOT a configuration error.  The two states
    must not collapse into each other or the loud failure becomes noise and gets ignored."""
    s = fi.ingest_cash_transactions(xml_text=_statement([]))
    assert s["ok"] is True
    assert s["section_present"] is True
    assert s["cash_rows"] == 0
    missing = fi.ingest_cash_transactions(xml_text=_statement(with_cash_section=False))
    assert missing["ok"] is False and missing["section_present"] is False


def test_ingest_flex_reports_the_missing_section_without_failing_the_trade_run(tmp_path, capsys):
    """A missing cash section must not turn a healthy trade-archive run into a failure (the
    scheduled job's success signal would invert), but it MUST be shouted to stderr so the
    .err log carries it with nobody having to opt in to noticing."""
    s = fi.ingest_flex(xml_text=_statement(with_cash_section=False),
                       ddir=str(tmp_path), dry_run=True)
    assert s["ok"] is True                                # trade ingest unaffected
    assert s["cash_ledger"]["ok"] is False
    assert s["cash_ledger"]["ledger_written"] is False
    err = capsys.readouterr().err
    assert "cash ledger NOT updated" in err
    assert "Cash Transactions" in err


def test_unparseable_xml_is_not_reported_as_a_missing_section():
    """A truncated download is a different failure from a misconfigured query, and conflating
    them would send the operator to the IBKR UI to fix something that is not broken."""
    s = fi.parse_cash_transactions("<FlexQueryResponse><notclosed>")
    assert s["parsed_ok"] is False
    assert s["section_present"] is False
    assert "error" in s
    out = fi.ingest_cash_transactions(xml_text="<FlexQueryResponse><notclosed>")
    assert out["ok"] is False
    assert "action_required" not in out


# ======================================================================= 4. idempotency
def test_two_runs_append_once(ledger_isolation):
    first = fi.ingest_cash_transactions(xml_text=_statement(ALL_ROWS))
    assert first["ledger"]["appended"] == 7               # control: run 1 really did work
    second = fi.ingest_cash_transactions(xml_text=_statement(ALL_ROWS))
    assert second["ok"] is True
    assert second["ledger"]["appended"] == 0
    assert second["ledger"]["duplicates"] == 7
    rows = _read_ledger(ledger_isolation)
    assert len(rows) == 7
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))


def test_rerun_with_new_rows_appends_only_the_new_ones(ledger_isolation):
    fi.ingest_cash_transactions(xml_text=_statement([DEPOSIT, DIVIDEND]))
    s = fi.ingest_cash_transactions(xml_text=_statement([DEPOSIT, DIVIDEND, WITHDRAWAL]))
    assert s["ledger"]["appended"] == 1
    assert s["ledger"]["duplicates"] == 2
    rows = _read_ledger(ledger_isolation)
    assert len(rows) == 3
    assert _rows_by_id(rows)["90000000003"]["type"] == "withdrawal"


def test_idempotency_survives_a_missing_transaction_id(ledger_isolation):
    """Some statements omit transactionID.  Falling back to a content hash keeps a re-run from
    duplicating -- an identity that only works when IBKR is generous is not an identity."""
    noid = _cash(type="Deposits/Withdrawals", amount="500", settleDate="20260903",
                 description="ELECTRONIC FUND TRANSFER")
    fi.ingest_cash_transactions(xml_text=_statement([noid]))
    again = fi.ingest_cash_transactions(xml_text=_statement([noid]))
    assert again["ledger"]["appended"] == 0
    assert len(_read_ledger(ledger_isolation)) == 1


def test_negative_control_two_genuinely_distinct_rows_are_both_kept(ledger_isolation):
    """NEGATIVE CONTROL for dedup: the key must not be so coarse that it swallows real, distinct
    cash events.  Two $500 deposits on different dates are two deposits."""
    a = _cash(type="Deposits/Withdrawals", amount="500", settleDate="20260803",
              transactionID="90000000010", description="ELECTRONIC FUND TRANSFER")
    b = _cash(type="Deposits/Withdrawals", amount="500", settleDate="20260903",
              transactionID="90000000011", description="ELECTRONIC FUND TRANSFER")
    s = fi.ingest_cash_transactions(xml_text=_statement([a, b]))
    assert s["ledger"]["appended"] == 2
    assert len(_read_ledger(ledger_isolation)) == 2
    # and with no transactionID at all, distinct dates still stay distinct
    c = _cash(type="Dividends", amount="12.50", settleDate="20260814", symbol="SPY")
    d = _cash(type="Dividends", amount="12.50", settleDate="20260914", symbol="SPY")
    s2 = fi.ingest_cash_transactions(xml_text=_statement([c, d]))
    assert s2["ledger"]["appended"] == 2


def test_restated_amount_is_a_conflict_not_a_duplicate_and_not_an_overwrite(ledger_isolation):
    """IBKR restating a cash event is a fact a human should see.  Appending would double-count;
    rewriting the line would destroy the evidence.  Neither happens."""
    fi.ingest_cash_transactions(xml_text=_statement([DEPOSIT]))
    restated = _cash(type="Deposits/Withdrawals", amount="450", settleDate="20260803",
                     transactionID="90000000001", description="ELECTRONIC FUND TRANSFER")
    s = fi.ingest_cash_transactions(xml_text=_statement([restated]))
    assert s["ledger"]["appended"] == 0
    assert len(s["ledger"]["conflicts"]) == 1
    assert "disagree" in s["note"]
    rows = _read_ledger(ledger_isolation)
    assert len(rows) == 1
    assert rows[0]["amount"] == 500.0                     # on-disk row untouched


def test_dry_run_writes_nothing(ledger_isolation):
    s = fi.ingest_cash_transactions(xml_text=_statement(ALL_ROWS), dry_run=True)
    assert s["ok"] is True
    assert s["ledger"]["appended"] == 7                   # it says what it WOULD do
    assert s["ledger_written"] is False
    assert not os.path.exists(ledger_isolation)


def test_write_is_append_only_and_preserves_unrelated_hand_written_rows(ledger_isolation):
    """The ledger is allowed to contain rows a human wrote.  An ingest must add to it, never
    rewrite it out from under them."""
    with open(ledger_isolation, "w") as fh:
        fh.write(json.dumps({"date": "2026-07-26", "amount": 500.0, "type": "contribution",
                             "note": "hand-recorded opening ACH"}) + "\n")
    s = fi.ingest_cash_transactions(xml_text=_statement([DEPOSIT]))
    rows = _read_ledger(ledger_isolation)
    assert len(rows) == 2
    assert rows[0]["note"] == "hand-recorded opening ACH"  # still line 1, byte-identical intent
    assert s["ledger"]["existing"] == 1


# ============================================================ 5. malformed rows are survivable
def test_malformed_rows_are_skipped_without_losing_the_rest(ledger_isolation):
    bad_amount = _cash(type="Dividends", amount="not-a-number", settleDate="20260810",
                       transactionID="90000000020")
    no_amount = _cash(type="Dividends", settleDate="20260811", transactionID="90000000021")
    no_date = _cash(type="Deposits/Withdrawals", amount="100", transactionID="90000000022")
    junk_date = _cash(type="Other Fees", amount="-2", settleDate="garbage",
                      dateTime="garbage", reportDate="garbage", transactionID="90000000023")
    zero = _cash(type="Other Fees", amount="0", settleDate="20260812",
                 transactionID="90000000024")
    s = fi.ingest_cash_transactions(
        xml_text=_statement([bad_amount, DEPOSIT, no_amount, DIVIDEND, no_date, junk_date,
                             zero, WITHDRAWAL]))
    assert s["ok"] is True
    assert s["cash_rows"] == 3                            # the three good rows survived
    assert s["skipped"] == 5
    assert {r["id"] for r in _read_ledger(ledger_isolation)} == {
        "90000000001", "90000000002", "90000000003"}
    assert len(s["skipped_detail"]) == 5


def test_negative_control_the_malformed_fixture_would_otherwise_count(ledger_isolation):
    """NEGATIVE CONTROL: prove the 5 skipped rows are skipped because they are MALFORMED, not
    because the parser drops rows generally.  Repaired, the identical rows all land."""
    repaired = [
        _cash(type="Dividends", amount="1", settleDate="20260810", transactionID="90000000020"),
        _cash(type="Dividends", amount="1", settleDate="20260811", transactionID="90000000021"),
        _cash(type="Deposits/Withdrawals", amount="100", settleDate="20260809",
              transactionID="90000000022"),
        _cash(type="Other Fees", amount="-2", settleDate="20260813",
              transactionID="90000000023"),
        _cash(type="Other Fees", amount="-0.01", settleDate="20260812",
              transactionID="90000000024"),
    ]
    s = fi.ingest_cash_transactions(xml_text=_statement(repaired))
    assert s["cash_rows"] == 5
    assert s["skipped"] == 0


def test_zero_amount_rows_are_dropped_on_purpose(ledger_isolation):
    """pnl_net's 'no ledger rows AND |residual| < epsilon -> noise' branch keys on ROW
    PRESENCE.  A $0.00 row moves no cash but WOULD reclassify an otherwise-quiet period, so it
    is dropped rather than emitted."""
    zero = _cash(type="Other Fees", amount="0.00", settleDate="20260812",
                 transactionID="90000000030")
    s = fi.ingest_cash_transactions(xml_text=_statement([zero]))
    assert s["ok"] is True
    assert s["cash_rows"] == 0
    assert not os.path.exists(ledger_isolation)


def test_a_single_malformed_row_cannot_abort_the_statement(ledger_isolation):
    """Failure isolation, stated directly: one bad row must not cost the other N-1."""
    rows = [DEPOSIT, _cash(type="Dividends", amount="", settleDate=""), DIVIDEND]
    s = fi.ingest_cash_transactions(xml_text=_statement(rows))
    assert s["cash_rows"] == 2
    assert s["skipped"] == 1


# =============================================================== 6. dates, FX, level of detail
def test_settle_date_is_preferred_and_the_source_is_recorded(ledger_isolation):
    """pnl_net matches a row into the period (previous snapshot date, this snapshot date], so
    the date must be when the cash LANDED, i.e. settleDate."""
    r = _cash(type="Dividends", amount="5", dateTime="20260814;202000", settleDate="20260818",
              reportDate="20260814", transactionID="90000000040", symbol="SPY")
    fi.ingest_cash_transactions(xml_text=_statement([r]))
    row = _read_ledger(ledger_isolation)[0]
    assert row["date"] == "2026-08-18"
    assert row["date_source"] == "settleDate"


def test_date_falls_back_when_settle_date_is_absent(ledger_isolation):
    r = _cash(type="Dividends", amount="5", dateTime="20260814;202000",
              reportDate="20260813", transactionID="90000000041", symbol="SPY")
    fi.ingest_cash_transactions(xml_text=_statement([r]))
    row = _read_ledger(ledger_isolation)[0]
    assert row["date"] == "2026-08-14"
    assert row["date_source"] == "dateTime"


def test_dates_are_iso_so_pnl_nets_string_window_comparison_works(ledger_isolation):
    """pnl_net compares dates as STRINGS (`lo < d <= hi`).  Anything but zero-padded ISO
    silently lands the row in the wrong period, or in none."""
    fi.ingest_cash_transactions(xml_text=_statement(ALL_ROWS))
    for row in _read_ledger(ledger_isolation):
        assert len(row["date"]) == 10 and row["date"][4] == "-" and row["date"][7] == "-"
        assert row["date"] == row["date"].strip()


def test_non_base_currency_is_converted_with_ibkrs_own_rate(ledger_isolation):
    """net_liq is in the base currency, so the ledger must be too -- using IBKR's fxRateToBase.
    No rate is ever invented."""
    r = _cash(type="Dividends", amount="100", currency="CAD", fxRateToBase="0.73",
              settleDate="20260901", transactionID="90000000050", symbol="ENB")
    fi.ingest_cash_transactions(xml_text=_statement([r]))
    row = _read_ledger(ledger_isolation)[0]
    assert row["amount"] == pytest.approx(73.0)
    assert row["amount_original"] == 100.0
    assert row["currency"] == "CAD"
    assert row["fx_rate_to_base"] == 0.73
    assert "fx_unconverted" not in row


def test_unconvertible_foreign_currency_is_flagged_not_silently_treated_as_usd(ledger_isolation):
    r = _cash(type="Dividends", amount="100", currency="CAD", fxRateToBase="",
              settleDate="20260901", transactionID="90000000051", symbol="ENB")
    fi.ingest_cash_transactions(xml_text=_statement([r]))
    row = _read_ledger(ledger_isolation)[0]
    assert row["fx_unconverted"] is True
    assert "UNCONVERTED CAD" in row["note"]


def test_summary_and_detail_rows_do_not_double_count(ledger_isolation):
    """Flex can emit the same cash event at two granularities.  Keeping both would double every
    dividend in the report."""
    detail = _cash(type="Dividends", amount="12.50", settleDate="20260814",
                   transactionID="90000000060", symbol="SPY", levelOfDetail="DETAIL")
    summary = _cash(type="Dividends", amount="12.50", settleDate="20260814",
                    transactionID="90000000060", symbol="SPY", levelOfDetail="SUMMARY")
    s = fi.ingest_cash_transactions(xml_text=_statement([detail, summary]))
    assert s["cash_rows"] == 1
    assert s["level_of_detail"] == "DETAIL"
    assert s["income_total"] == 12.50


def test_summary_only_statement_is_still_ingested(ledger_isolation):
    """Preferring DETAIL must not mean DISCARDING everything when only SUMMARY exists."""
    summary = _cash(type="Dividends", amount="12.50", settleDate="20260814",
                    transactionID="90000000061", symbol="SPY", levelOfDetail="SUMMARY")
    s = fi.ingest_cash_transactions(xml_text=_statement([summary]))
    assert s["cash_rows"] == 1
    assert s["income_total"] == 12.50


# ==================================================== 7. differential: existing behaviour held
def test_parse_statement_contract_is_unchanged():
    """parse_statement is used by live code paths.  Its shape is pinned: {fills, meta}, nothing
    added, nothing renamed."""
    parsed = fi.parse_statement(_statement(ALL_ROWS))
    assert set(parsed.keys()) == {"fills", "meta"}
    assert isinstance(parsed["fills"], list) and isinstance(parsed["meta"], dict)


def test_cash_transactions_do_not_leak_into_fills():
    """The differential that matters most: adding a CashTransactions section changes NOTHING
    about the trade side.  A cash row landing in `fills` would be fabricated trade history."""
    without = fi.parse_statement(_statement(with_cash_section=False))
    with_cash = fi.parse_statement(_statement(ALL_ROWS))
    assert without == with_cash
    assert len(with_cash["fills"]) == 1
    assert all(f.get("exec_id") for f in with_cash["fills"])


@pytest.mark.skipif(not os.path.exists(_SAMPLE), reason="private Flex XML fixture not present")
def test_parse_statement_over_the_real_sample_is_bit_for_bit_identical():
    """Pinned against output captured from the UNMODIFIED module immediately before this work.
    If the cash-transaction changes perturbed the trade parse by even one field, this fails."""
    import hashlib
    with open(_SAMPLE) as fh:
        parsed = fi.parse_statement(fh.read())
    assert len(parsed["fills"]) == _BASELINE_FILLS
    assert parsed["meta"] == _BASELINE_META
    blob = json.dumps(parsed, sort_keys=True, default=str)
    assert hashlib.sha256(blob.encode()).hexdigest() == _BASELINE_SHA


def test_negative_control_the_differential_sha_can_actually_fail():
    """NEGATIVE CONTROL: prove the pinned sha above is a real discriminator and not, say, the
    hash of an empty structure that anything would match."""
    import hashlib
    blob = json.dumps({"fills": [], "meta": {}}, sort_keys=True, default=str)
    assert hashlib.sha256(blob.encode()).hexdigest() != _BASELINE_SHA
    assert _BASELINE_FILLS > 0


def test_ingest_flex_still_returns_every_pre_existing_key(tmp_path):
    """`ok` must still mean 'the TRADE ingest worked', and the pre-existing summary keys must
    all still be there -- run_flex_ingest.sh reads them by name."""
    s = fi.ingest_flex(xml_text=_statement(ALL_ROWS), ddir=str(tmp_path), dry_run=True)
    for key in ("ok", "dataset_path", "meta", "fills", "contracts", "strategies",
                "flex_trade_rows", "flex_position_rows", "reconcile", "summary"):
        assert key in s, key
    assert s["ok"] is True
    assert s["fills"] == 1
    assert s["cash_ledger"]["ok"] is True                 # additive, and it ran


def test_cash_ledger_can_be_disabled(tmp_path, ledger_isolation):
    s = fi.ingest_flex(xml_text=_statement(ALL_ROWS), ddir=str(tmp_path), dry_run=True,
                       cash_ledger=False)
    assert s["ok"] is True
    assert s["cash_ledger"]["ok"] is None
    assert not os.path.exists(ledger_isolation)


def test_ingest_flex_writes_the_ledger_on_a_real_run(tmp_path, ledger_isolation):
    """Zero-friction: the ALREADY-SCHEDULED flex ingest maintains the ledger, so nobody has to
    remember to run a second job."""
    s = fi.ingest_flex(xml_text=_statement(ALL_ROWS), ddir=str(tmp_path))
    assert s["cash_ledger"]["ok"] is True
    assert s["cash_ledger"]["ledger_written"] is True
    assert len(_read_ledger(ledger_isolation)) == 7


# =================================================================== 8. paths, CLI, safety
def test_ledger_path_resolution_order(monkeypatch, tmp_path):
    monkeypatch.setenv(fi.CASH_LEDGER_ENV, str(tmp_path / "from_env.jsonl"))
    assert fi.cash_ledger_path() == str(tmp_path / "from_env.jsonl")
    assert fi.cash_ledger_path(str(tmp_path / "explicit.jsonl")) == str(tmp_path / "explicit.jsonl")
    monkeypatch.delenv(fi.CASH_LEDGER_ENV)
    assert fi.cash_ledger_path() == fi.DEFAULT_CASH_LEDGER


def test_default_ledger_path_is_the_one_pnl_net_reads():
    """A ledger written anywhere else is a ledger the tracker never sees."""
    assert fi.DEFAULT_CASH_LEDGER == os.path.join(os.path.expanduser("~"), "contributions.jsonl")


def test_cli_cash_only_exits_non_zero_when_the_section_is_missing(tmp_path, capsys):
    xml = tmp_path / "stmt.xml"
    xml.write_text(_statement(with_cash_section=False))
    rc = fi._main(["--cash-only", "--xml", str(xml),
                   "--cash-ledger", str(tmp_path / "ledger.jsonl")])
    assert rc == 1
    assert not os.path.exists(tmp_path / "ledger.jsonl")
    err = capsys.readouterr().err
    assert "Cash Transactions" in err


def test_cli_cash_only_exits_zero_and_writes_when_the_section_is_present(tmp_path, capsys):
    """NEGATIVE CONTROL for the exit code: rc==1 above is caused by the missing section, not by
    --cash-only being broken."""
    xml = tmp_path / "stmt.xml"
    xml.write_text(_statement(ALL_ROWS))
    ledger = tmp_path / "ledger.jsonl"
    rc = fi._main(["--cash-only", "--xml", str(xml), "--cash-ledger", str(ledger)])
    assert rc == 0
    assert len(_read_ledger(str(ledger))) == 7
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["cash_rows"] == 7


def test_cash_path_makes_no_network_call_when_xml_is_supplied():
    """READ-ONLY is the standing constraint on this module; with XML injected there must be no
    HTTP at all, so a test can never touch IBKR."""
    def boom(url, timeout=90):                            # pragma: no cover - must not run
        raise AssertionError(f"unexpected network call to {url}")
    s = fi.ingest_cash_transactions(xml_text=_statement(ALL_ROWS), opener=boom)
    assert s["ok"] is True


def test_token_never_leaks_through_the_cash_path(monkeypatch):
    monkeypatch.delenv("IBKR_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("IBKR_FLEX_QUERY_ID", raising=False)

    def opener(url, timeout=90):
        raise RuntimeError(f"upstream said no, url was {url}")

    s = fi.ingest_cash_transactions(token="SUPERSECRET", query_id="1562555", opener=opener)
    assert s["ok"] is False
    assert "SUPERSECRET" not in json.dumps(s, default=str)


def test_no_ledger_row_is_ever_fabricated(ledger_isolation):
    """Every emitted row traces to an attribute IBKR actually sent.  Nothing is inferred, and
    the count out never exceeds the count in."""
    s = fi.ingest_cash_transactions(xml_text=_statement(ALL_ROWS))
    rows = _read_ledger(ledger_isolation)
    assert len(rows) == s["raw_rows"] == len(ALL_ROWS)
    for row in rows:
        assert row["source"] == "ibkr_flex"
        assert row["ibkr_type"]
        assert row["account"] == "U00000000"
