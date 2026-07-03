"""Unit tests for exitmgr.flex_ingest -- IBKR Flex Web Service history ingest (2026-07-03).

All offline: the live statement is replaced by a saved real Flex XML fixture (tests/flex_sample.xml,
46 real fills) and the HTTP layer is mocked. Covers XML parse, open/close + commission-sign
normalization, per-contract pairing/retag, backfill supersede/reconcile, exec-id dedup vs
reqExecutions rows, open-position snapshots, no-fabrication of reasoning, token redaction, the
SendRequest/GetStatement fetch flow (incl. the 1019 'in progress' warn), and idempotency."""
import json
import os

import pytest

from exitmgr import flex_ingest as fi

_HERE = os.path.dirname(__file__)
_SAMPLE = os.path.join(_HERE, "flex_sample.xml")


@pytest.fixture
def sample_xml():
    with open(_SAMPLE) as f:
        return f.read()


# ------------------------------------------------------------------ dt + creds + redaction
def test_parse_flex_dt():
    assert fi._parse_flex_dt("20260629;133649") == "2026-06-29T13:36:49"
    assert fi._parse_flex_dt("20260629") == "2026-06-29"
    assert fi._parse_flex_dt(None) is None


def test_redact_hides_token():
    assert "SECRETTOKEN" not in fi._redact("boom SECRETTOKEN boom", "SECRETTOKEN")
    assert fi._redact("hello", None) == "hello"


def test_load_flex_creds_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("IBKR_FLEX_TOKEN", raising=False)
    monkeypatch.delenv("IBKR_FLEX_QUERY_ID", raising=False)
    p = tmp_path / ".env"
    p.write_text('IBKR_FLEX_TOKEN="abc123"\nIBKR_FLEX_QUERY_ID=1562555\n# comment\n')
    token, qid = fi.load_flex_creds(str(p))
    assert token == "abc123" and qid == "1562555"


def test_load_flex_creds_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "envtok")
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "999")
    token, qid = fi.load_flex_creds(str(tmp_path / "missing.env"))
    assert token == "envtok" and qid == "999"


# ------------------------------------------------------------------ parse
def test_parse_statement_counts(sample_xml):
    parsed = fi.parse_statement(sample_xml)
    assert len(parsed["fills"]) == 46
    assert parsed["meta"]["accountId"] == "U00000000"
    assert parsed["meta"]["period"] == "Last365CalendarDays"


def test_normalize_open_vs_close_and_commission_sign(sample_xml):
    fills = fi.parse_statement(sample_xml)["fills"]
    # opening fill -> realized_pnl_ib None; closing fill -> real fifoPnlRealized
    opens = [f for f in fills if f["open_close"] == "O"]
    closes = [f for f in fills if f["open_close"] == "C"]
    assert all(f["realized_pnl_ib"] is None for f in opens)
    assert all(f["realized_pnl_ib"] is not None for f in closes)
    # Flex reports commission negative; we normalize to a POSITIVE fee (like reqExecutions)
    assert all((f["commission"] is None or f["commission"] >= 0) for f in fills)
    # shares are unsigned; underlying symbol (not the OCC string) is the symbol
    sndk = [f for f in fills if f["symbol"] == "SNDK"]
    assert sndk and all(f["shares"] >= 0 for f in sndk)
    assert all(f["mult"] == 100.0 for f in sndk)


def test_build_rows_shape(sample_xml):
    parsed = fi.parse_statement(sample_xml)
    built = fi.build_flex_rows(parsed["fills"], parsed["meta"])
    assert len(built["trade_rows"]) == 18
    assert len(built["position_rows"]) == 2  # Agilent A C135 / C150 spread still open
    # every flex row is honestly tagged + carries NO fabricated reasoning
    for r in built["trade_rows"] + built["position_rows"]:
        assert r["source"] == "flex_history"
        assert r["reasoning_available"] is False
        assert r["decision"] is None
        if r["kind"] == "trade":
            assert r["entry"]["thesis"] is None and r["entry"]["conviction"] is None
    # open positions are snapshots, never fake closes
    for p in built["position_rows"]:
        assert p["kind"] == "position" and p["status"] == "open"
        assert p["symbol"] == "A"


# ------------------------------------------------------------------ reconcile / supersede
def _backfill_row(uid, inst, symbol, pnl):
    return {"schema": "trade_dataset.v2", "kind": "trade", "ts": "2026-07-03T00:00:00+00:00",
            "trade_uid": uid, "trade_instance_uid": inst, "backfilled": True,
            "backfill_source": "backfill:exits.log+trades.log", "symbol": symbol,
            "decision": None, "entry": {"symbol": symbol}, "close": {"realized_pnl": pnl,
            "commission_unknown": True}, "_dedup_key": f"trade_instance:{inst}"}


def _seed(ddir, rows):
    with open(fi._tc.dataset_path(ddir), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_reconcile_supersedes_backfill(sample_xml, tmp_path):
    ddir = str(tmp_path)
    # the 3 real backfilled estimate rows (SNDK / NOK C14 / NOK C25) by their true uuid5 identities
    _seed(ddir, [
        _backfill_row("5c52ab43-302a-5dc0-9a64-e4cb71e513d6",
                      "6a02dd4d-c04b-54e4-b8e8-9d904b3b2e0b", "SNDK", -287.82),
        _backfill_row("bb8ba252-c0d9-5883-bb16-070d4e2635e7",
                      "f6504e78-ff33-57be-acb5-19a00b7a36ce", "NOK", -121.89),
        _backfill_row("7e915bd5-17df-5a9e-826c-4ecd9754d001",
                      "7e955f51-4815-5e7e-8921-85703c4b329b", "NOK", -2.0),
    ])
    s = fi.ingest_flex(xml_text=sample_xml, ddir=ddir)
    assert s["ok"]
    rc = s["reconcile"]
    assert rc["superseded"] == 3          # all 3 estimate rows replaced by richer Flex rows
    assert rc["appended_trades"] == 18
    assert rc["appended_positions"] == 2
    assert rc["final_rows"] == 20         # one best row per real trade; no estimate rows remain
    # the file no longer contains any backfilled estimate row
    with open(fi._tc.dataset_path(ddir)) as f:
        rows = [json.loads(x) for x in f if x.strip()]
    assert not any(r.get("backfilled") for r in rows)
    assert all(r["source"] == "flex_history" for r in rows)


def test_reconcile_idempotent(sample_xml, tmp_path):
    ddir = str(tmp_path)
    _seed(ddir, [_backfill_row("5c52ab43-302a-5dc0-9a64-e4cb71e513d6",
                               "6a02dd4d-c04b-54e4-b8e8-9d904b3b2e0b", "SNDK", -287.82)])
    fi.ingest_flex(xml_text=sample_xml, ddir=ddir)
    n1 = sum(1 for _ in open(fi._tc.dataset_path(ddir)))
    s2 = fi.ingest_flex(xml_text=sample_xml, ddir=ddir)  # second run
    n2 = sum(1 for _ in open(fi._tc.dataset_path(ddir)))
    assert s2["reconcile"]["superseded"] == 0
    assert s2["reconcile"]["appended_trades"] == 0
    assert s2["reconcile"]["appended_positions"] == 0
    assert n1 == n2  # a second run adds 0


def test_execid_dedup_vs_reqexecutions(sample_xml, tmp_path):
    """A Flex row whose execIDs already appear in a real reqExecutions/app row is NOT re-added."""
    ddir = str(tmp_path)
    parsed = fi.parse_statement(sample_xml)
    built = fi.build_flex_rows(parsed["fills"], parsed["meta"])
    # take one flex trade row's exec_ids and plant them on a pre-existing app row
    victim = next(r for r in built["trade_rows"] if r["kind"] == "trade")
    exec_ids = victim["provenance"]["exec_ids"]
    app_row = {"schema": "trade_dataset.v2", "kind": "trade", "source": "app",
               "symbol": victim["symbol"], "ts": "2026-07-01T00:00:00+00:00",
               "provenance": {"exec_ids": exec_ids}, "_dedup_key": "app:preexisting"}
    _seed(ddir, [app_row])
    s = fi.ingest_flex(xml_text=sample_xml, ddir=ddir)
    assert s["reconcile"]["skipped_execdup"] >= 1
    assert s["reconcile"]["superseded"] == 0  # an app row is NEVER superseded


def test_dry_run_writes_nothing(sample_xml, tmp_path):
    ddir = str(tmp_path)
    _seed(ddir, [_backfill_row("5c52ab43-302a-5dc0-9a64-e4cb71e513d6",
                               "6a02dd4d-c04b-54e4-b8e8-9d904b3b2e0b", "SNDK", -287.82)])
    fi.ingest_flex(xml_text=sample_xml, ddir=ddir, dry_run=True)
    with open(fi._tc.dataset_path(ddir)) as f:
        rows = [json.loads(x) for x in f if x.strip()]
    assert len(rows) == 1 and rows[0].get("backfilled")  # untouched


# ------------------------------------------------------------------ HTTP fetch flow (mocked)
_SEND_OK = ("<FlexStatementResponse timestamp='x'><Status>Success</Status>"
            "<ReferenceCode>REF999</ReferenceCode>"
            "<Url>https://x/GetStatement</Url></FlexStatementResponse>")
_INPROGRESS = ("<FlexStatementResponse timestamp='x'><Status>Warn</Status>"
               "<ErrorCode>1019</ErrorCode>"
               "<ErrorMessage>Statement generation in progress.</ErrorMessage></FlexStatementResponse>")
_HARDERR = ("<FlexStatementResponse timestamp='x'><Status>Fail</Status>"
            "<ErrorCode>1003</ErrorCode><ErrorMessage>bad token</ErrorMessage></FlexStatementResponse>")


def test_fetch_flow_polls_until_ready(sample_xml):
    calls = {"n": 0}

    def opener(url):
        calls["n"] += 1
        if "SendRequest" in url:
            return _SEND_OK
        # GetStatement: two 'in progress' warns, then the ready statement
        return _INPROGRESS if calls["n"] <= 3 else sample_xml

    xml = fi.fetch_statement_xml("TOK", "1562555", opener=opener, sleep=lambda *_: None)
    assert "<FlexQueryResponse" in xml
    assert calls["n"] >= 3  # sent + >=2 polls


def test_poll_raises_on_hard_error():
    def opener(url):
        return _HARDERR
    with pytest.raises(RuntimeError):
        fi.poll_statement("https://x/GetStatement", "REF", "TOK", opener=opener,
                          tries=2, sleep=lambda *_: None)


def test_send_request_error_is_redacted():
    def opener(url):
        return _HARDERR
    with pytest.raises(RuntimeError) as ei:
        fi.send_request("SUPERSECRET", "1562555", opener=opener)
    assert "SUPERSECRET" not in str(ei.value)


def test_ingest_end_to_end_with_mocked_http(sample_xml, tmp_path, monkeypatch):
    ddir = str(tmp_path)
    monkeypatch.setenv("IBKR_FLEX_TOKEN", "tok")
    monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "1562555")

    def opener(url):
        return _SEND_OK if "SendRequest" in url else sample_xml

    s = fi.ingest_flex(ddir=ddir, opener=opener, sleep=lambda *_: None)
    assert s["ok"] and s["fills"] == 46
    assert s["flex_trade_rows"] == 18 and s["flex_position_rows"] == 2
