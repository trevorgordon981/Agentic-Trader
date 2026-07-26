"""Contract tests for the CREDIT limb of TradeIdea (CREDIT_PATH_SPEC.md section 1).

Two jobs, in order of importance:

1. **Prove the debit path is unchanged.** Every existing row must parse byte-identically. This
   is checked twice -- against a frozen golden snapshot AND, differentially, against the
   pre-change parser recovered from git.
2. **Prove the credit limb only ever admits a cash-secured put.** Invariant 1 of the spec is
   "NEVER sell a naked call"; the enforcement point is that an unbounded-loss structure is
   *unparseable*, not merely discouraged. A short call, strangle or condor with otherwise
   perfect credit fields must still be dropped.

Each guard carries a NEGATIVE CONTROL: the guard is deliberately disabled and the test asserts
the bad payload then *does* get through. Without that, a rejection test proves nothing -- an
idea can be dropped for an unrelated reason (a typo'd key, a missing field) and the test still
goes green while the real guard does nothing.
"""

import dataclasses
import json
import pathlib
import subprocess
import types

import pytest

from exitmgr import strategist
from exitmgr.strategist import CSP_STRUCTURE, TradeIdea, normalize_debit, parse_ideas


# --------------------------------------------------------------------------------------------
# payload helpers
# --------------------------------------------------------------------------------------------

def wrap(*trades):
    return json.dumps({"trades": list(trades)})


def csp(**over):
    """A valid cash-secured put: 1x NVDA 120P, $1.85 credit -> $185 on $12,000 collateral."""
    t = {
        "underlying": "NVDA",
        "is_index": False,
        "direction": "bullish",
        "structure": "cash secured put",
        "side": "credit",
        "target_dte": 30,
        "target_delta": 0.25,
        "strike": 120.0,
        "collateral_usd": 12000.0,
        "net_credit_usd": 185.0,
        "max_loss_usd": 11815.0,
        "conviction": 7,
        "thesis": "sell the 120 put into support",
    }
    t.update(over)
    return t


def debit(**over):
    t = {
        "underlying": "SPY",
        "is_index": True,
        "direction": "bullish",
        "structure": "long call",
        "target_dte": 45,
        "target_delta": 0.35,
        "est_debit_usd": 420.0,
        "conviction": 6,
        "thesis": "trend continuation",
    }
    t.update(over)
    return t


# --------------------------------------------------------------------------------------------
# 1. THE HAPPY PATH -- a valid CSP survives with every field intact
# --------------------------------------------------------------------------------------------

def test_valid_csp_parses_with_all_fields_intact():
    ideas = parse_ideas(wrap(csp()))
    assert len(ideas) == 1, "a fully specified cash-secured put must parse"
    i = ideas[0]
    assert i.side == "credit"
    assert i.structure == CSP_STRUCTURE
    assert i.underlying == "NVDA"
    assert i.strike == 120.0
    assert i.collateral_usd == 12000.0
    assert i.net_credit_usd == 185.0
    assert i.max_loss_usd == 11815.0
    assert i.conviction == 7
    assert i.target_dte == 30
    # est_debit_usd is NOT required and must NOT be invented.
    assert i.est_debit_usd == 0.0


def test_credit_and_debit_ideas_coexist_in_one_payload():
    ideas = parse_ideas(wrap(debit(), csp()))
    assert [i.side for i in ideas] == ["debit", "credit"]
    assert ideas[0].est_debit_usd == 420.0 and ideas[0].net_credit_usd == 0.0
    assert ideas[1].est_debit_usd == 0.0 and ideas[1].net_credit_usd == 185.0


def test_est_debit_on_a_credit_idea_is_ignored_not_used():
    # A model that hallucinates a debit onto a credit idea must not have it believed.
    ideas = parse_ideas(wrap(csp(est_debit_usd=999.0)))
    assert len(ideas) == 1 and ideas[0].est_debit_usd == 0.0


def test_csp_structure_is_canonicalised():
    for raw in ("Cash Secured Put", "  CASH SECURED PUT  ", "cash  secured\tput"):
        ideas = parse_ideas(wrap(csp(structure=raw)))
        assert len(ideas) == 1, "case/whitespace variants of the CSP structure must parse: %r" % raw
        assert ideas[0].structure == CSP_STRUCTURE


# --------------------------------------------------------------------------------------------
# 2. INVARIANT 1 -- the only permitted short is a cash-secured put
# --------------------------------------------------------------------------------------------

# Every one of these carries PERFECT credit fields (present, positive, arithmetic exact). The
# ONLY thing wrong is the structure, so a pass here can only mean the structure gate fired.
UNBOUNDED_OR_UNDEFINED = [
    "naked call",
    "short call",
    "sell call",
    "covered call",
    "short strangle",
    "strangle",
    "iron condor",
    "credit spread",
    "put credit spread",
    "short straddle",
    "ratio spread",
    "cash secured call",     # near-miss on the canonical string
    "cash secured puts",     # plural near-miss
    "csp",
    "",
]


@pytest.mark.parametrize("structure", UNBOUNDED_OR_UNDEFINED)
def test_credit_idea_with_non_csp_structure_is_rejected(structure):
    # direction="bullish": since the S4 carve-out landed, an explicit BEARISH direction is
    # itself a rejection reason on the credit path, which would mask the structure gate and
    # make this test pass for the wrong reason.
    payload = wrap(csp(structure=structure, direction="bullish"))
    assert parse_ideas(payload) == [], (
        "side=credit with structure %r must be REJECTED -- the only permitted short is a "
        "cash-secured put (spec invariant 1: never sell a naked call)" % structure
    )


def test_naked_call_rejected_even_with_flawless_credit_fields():
    """The sharpest form of the invariant: nothing else is wrong with this idea."""
    t = csp(structure="naked call", direction="bullish",
            strike=200.0, collateral_usd=20000.0, net_credit_usd=500.0, max_loss_usd=19500.0)
    # sanity: the arithmetic assertion would be satisfied, so it cannot be the reason for the drop
    assert abs(t["max_loss_usd"] - (t["collateral_usd"] - t["net_credit_usd"])) < 0.01
    assert parse_ideas(wrap(t)) == []


def test_negative_control_structure_gate_is_what_rejects_naked_calls(monkeypatch):
    """NEGATIVE CONTROL. Disable ONLY the structure check and the naked call must sail through.
    If it still fails to parse, the tests above were passing for the wrong reason."""
    real = strategist._parse_credit_fields

    def lax(t):
        t2 = dict(t)
        t2["structure"] = CSP_STRUCTURE          # neuter the structure gate only
        out = real(t2)
        out["structure"] = str(t.get("structure", "")).strip().lower()
        return out

    monkeypatch.setattr(strategist, "_parse_credit_fields", lax)
    ideas = parse_ideas(wrap(csp(structure="naked call", direction="bullish")))
    assert len(ideas) == 1 and ideas[0].structure == "naked call", (
        "negative control failed: with the structure gate disabled the naked call should parse, "
        "so the rejection tests above are not actually exercising that gate"
    )


# --------------------------------------------------------------------------------------------
# 3. REQUIRED CREDIT FIELDS -- present and > 0
# --------------------------------------------------------------------------------------------

CREDIT_FIELDS = ["strike", "collateral_usd", "net_credit_usd", "max_loss_usd"]


@pytest.mark.parametrize("field", CREDIT_FIELDS)
def test_credit_field_missing_is_rejected(field):
    t = csp()
    del t[field]
    assert parse_ideas(wrap(t)) == [], "credit idea missing %s must be dropped" % field


@pytest.mark.parametrize("field", CREDIT_FIELDS)
@pytest.mark.parametrize("bad", [0, 0.0, -1.0, -12000.0, None, "", "abc", float("nan"),
                                 float("inf"), float("-inf")])
def test_credit_field_non_positive_or_junk_is_rejected(field, bad):
    t = csp()
    t[field] = bad
    assert parse_ideas(wrap(t)) == [], (
        "credit idea with %s=%r must be dropped (required, finite, > 0)" % (field, bad))


def test_negative_control_required_fields_gate_is_live(monkeypatch):
    """NEGATIVE CONTROL: a parser that defaults missing credit fields to 0 lets them through."""
    def sloppy(t):
        return {
            "structure": CSP_STRUCTURE,
            "strike": float(t.get("strike") or 0.0),
            "collateral_usd": float(t.get("collateral_usd") or 0.0),
            "net_credit_usd": float(t.get("net_credit_usd") or 0.0),
            "max_loss_usd": float(t.get("max_loss_usd") or 0.0),
        }

    monkeypatch.setattr(strategist, "_parse_credit_fields", sloppy)
    t = csp()
    del t["collateral_usd"]
    ideas = parse_ideas(wrap(t))
    assert len(ideas) == 1 and ideas[0].collateral_usd == 0.0, (
        "negative control failed: the required-field tests are not exercising the real gate")


# --------------------------------------------------------------------------------------------
# 4. max_loss_usd == collateral_usd - net_credit_usd, within $0.01
# --------------------------------------------------------------------------------------------

def test_max_loss_arithmetic_mismatch_is_rejected():
    # off by $100 -- the classic "forgot to subtract the credit" bug
    assert parse_ideas(wrap(csp(max_loss_usd=11915.0))) == []
    # off by $0.02 -- just outside tolerance
    assert parse_ideas(wrap(csp(max_loss_usd=11815.02))) == []
    # understated risk is equally wrong
    assert parse_ideas(wrap(csp(max_loss_usd=1000.0))) == []


def test_max_loss_within_one_cent_is_accepted():
    for ml in (11815.0, 11815.009, 11814.991):
        ideas = parse_ideas(wrap(csp(max_loss_usd=ml)))
        assert len(ideas) == 1, "max_loss within $0.01 must be accepted: %r" % ml


def test_negative_control_arithmetic_assertion_is_live(monkeypatch):
    """NEGATIVE CONTROL: drop the arithmetic assertion and the mismatched idea parses."""
    real = strategist._parse_credit_fields

    def no_arith(t):
        t2 = dict(t)
        t2["max_loss_usd"] = float(t["collateral_usd"]) - float(t["net_credit_usd"])
        out = real(t2)
        out["max_loss_usd"] = float(t["max_loss_usd"])
        return out

    monkeypatch.setattr(strategist, "_parse_credit_fields", no_arith)
    ideas = parse_ideas(wrap(csp(max_loss_usd=11915.0)))
    assert len(ideas) == 1 and ideas[0].max_loss_usd == 11915.0, (
        "negative control failed: the arithmetic tests are not exercising the real assertion")


# --------------------------------------------------------------------------------------------
# 5. normalize_debit() MUST NOT touch the credit fields
# --------------------------------------------------------------------------------------------

def test_normalize_debit_would_inflate_a_small_credit_100x():
    """Establishes the hazard is real before proving we avoid it -- otherwise the next test is
    vacuous. normalize_debit rescales anything under $25 by 100x."""
    assert normalize_debit(1.85) == 185.0
    assert normalize_debit(18.0) == 1800.0
    assert normalize_debit(12.0) == 1200.0
    assert normalize_debit(185.0) == 185.0     # above the threshold, untouched


def test_small_net_credit_is_not_rescaled():
    """A $9 net credit on a $12 strike is legitimate -- x100 here would misstate the position."""
    t = csp(strike=12.0, collateral_usd=1200.0, net_credit_usd=9.0, max_loss_usd=1191.0)
    ideas = parse_ideas(wrap(t))
    assert len(ideas) == 1
    i = ideas[0]
    assert i.net_credit_usd == 9.0, "net_credit_usd was rescaled -- 100x position inflation"
    assert i.strike == 12.0, "strike was rescaled"
    assert i.collateral_usd == 1200.0
    assert i.max_loss_usd == 1191.0
    # and the arithmetic gate still holds on the un-rescaled values
    assert abs(i.max_loss_usd - (i.collateral_usd - i.net_credit_usd)) < 0.01


def test_sub_dollar_net_credit_survives():
    t = csp(strike=5.0, collateral_usd=500.0, net_credit_usd=0.5, max_loss_usd=499.5)
    ideas = parse_ideas(wrap(t))
    assert len(ideas) == 1 and ideas[0].net_credit_usd == 0.5


def test_negative_control_rescaling_a_credit_is_detectable(monkeypatch):
    """NEGATIVE CONTROL: a parser that DOES normalize_debit the credit fields must fail the
    assertions above -- proving those assertions would actually catch the 100x bug."""
    real = strategist._parse_credit_fields

    def rescaling(t):
        out = real(t)
        out["net_credit_usd"] = normalize_debit(out["net_credit_usd"])
        return out

    monkeypatch.setattr(strategist, "_parse_credit_fields", rescaling)
    ideas = parse_ideas(wrap(csp(strike=12.0, collateral_usd=1200.0,
                                 net_credit_usd=9.0, max_loss_usd=1191.0)))
    assert len(ideas) == 1 and ideas[0].net_credit_usd == 900.0, (
        "negative control failed: rescaling did not change the parsed value, so the "
        "no-rescale test cannot be detecting anything")


# --------------------------------------------------------------------------------------------
# 6. conviction stays required on the credit path
# --------------------------------------------------------------------------------------------

def test_credit_idea_without_conviction_is_dropped():
    t = csp()
    del t["conviction"]
    assert parse_ideas(wrap(t)) == [], "conviction remains required (strategist :189 KeyError)"


def test_credit_idea_with_junk_conviction_is_dropped():
    for bad in (None, "", "high", [], {}):
        assert parse_ideas(wrap(csp(conviction=bad))) == []


def test_credit_conviction_is_clamped_like_debit():
    assert parse_ideas(wrap(csp(conviction=99)))[0].conviction == 10
    assert parse_ideas(wrap(csp(conviction=-4)))[0].conviction == 1


def test_credit_idea_without_dte_or_delta_is_dropped():
    for field in ("target_dte", "target_delta"):
        t = csp()
        del t[field]
        assert parse_ideas(wrap(t)) == [], "%s remains required on the credit path" % field


# --------------------------------------------------------------------------------------------
# 7. THE SIDE DISCRIMINATOR
# --------------------------------------------------------------------------------------------

def test_side_absent_defaults_to_debit():
    ideas = parse_ideas(wrap(debit()))
    assert len(ideas) == 1 and ideas[0].side == "debit"


def test_side_explicit_debit_is_the_debit_path():
    ideas = parse_ideas(wrap(debit(side="debit")))
    assert len(ideas) == 1 and ideas[0].side == "debit" and ideas[0].est_debit_usd == 420.0


@pytest.mark.parametrize("bad_side", ["short", "sell", "premium", "CREDIT ", "cred", "long",
                                      "netcredit", 1, True, [], {}])
def test_unrecognised_side_is_dropped_not_silently_treated_as_debit(bad_side):
    if isinstance(bad_side, str) and bad_side.strip().lower() in ("debit", "credit"):
        pytest.skip("recognised after normalisation")
    assert parse_ideas(wrap(debit(side=bad_side))) == [], (
        "side=%r is not in the enumerated domain and must drop the idea rather than fall "
        "through to a live order path" % (bad_side,))


def test_side_case_and_whitespace_tolerated():
    assert parse_ideas(wrap(csp(side=" Credit ")))[0].side == "credit"
    assert parse_ideas(wrap(debit(side="DEBIT")))[0].side == "debit"


def test_debit_idea_carrying_stray_credit_fields_ignores_them():
    """A debit idea is unaffected by credit keys -- they must not leak into the debit path."""
    t = debit(collateral_usd=99999.0, net_credit_usd=777.0, max_loss_usd=1.0, strike=500.0)
    ideas = parse_ideas(wrap(t))
    assert len(ideas) == 1
    i = ideas[0]
    assert (i.side, i.collateral_usd, i.net_credit_usd, i.max_loss_usd, i.strike) == \
        ("debit", 0.0, 0.0, 0.0, 0.0)
    assert i.est_debit_usd == 420.0


# --------------------------------------------------------------------------------------------
# 8. THE DEBIT PATH IS UNCHANGED -- golden snapshot
# --------------------------------------------------------------------------------------------

# Payloads lifted from the existing suite (tests/test_strategist.py) plus the edge cases the
# parser is known to normalise. Expected values are frozen from the PRE-change parser.
DEBIT_CORPUS = [
    '{"trades":[{"underlying":"SPY","is_index":true,"direction":"bullish","structure":"long call","target_dte":7,"target_delta":0.35,"est_debit_usd":90,"conviction":4,"thesis":"trend up"}]}',
    'Sure, here is my idea:\n{"trades":[{"underlying":"qqq","direction":"bearish","structure":"long put","target_dte":5,"target_delta":0.3,"est_debit_usd":80,"conviction":3,"thesis":"weak"}]} hope that helps',
    '{"trades":[]}',
    "the market looks choppy, no JSON here",
    '{"trades":[{bad json]}',
    # NOTE 2026-07-26: this row used the placeholder structure "x" before the R4 allow-list.
    # "x" is now REFUSED on every path (see NEWLY_BANNED_BY_ALLOWLIST below, which keeps the
    # original bytes and asserts the change explicitly). The clamp/drop semantics this row
    # exists to freeze -- conviction>10 and delta>1 CLAMPED, bad direction and negative debit
    # DROPPED -- are unchanged and still covered here with a permitted structure.
    ('{"trades":['
     '{"underlying":"SPY","direction":"bullish","structure":"long call","target_dte":7,"target_delta":1.7,"est_debit_usd":50,"conviction":9,"thesis":"clamp"},'
     '{"underlying":"SPY","direction":"sideways","structure":"iron condor","target_dte":7,"target_delta":0.3,"est_debit_usd":50,"conviction":3,"thesis":"drop-dir"},'
     '{"underlying":"SPY","direction":"bullish","structure":"long call","target_dte":7,"target_delta":0.3,"est_debit_usd":-5,"conviction":3,"thesis":"drop-debit"}'
     ']}'),
    ('{"trades":['
     '{"underlying":"SPY","direction":"bullish","target_delta":0.3,"est_debit_usd":50,"conviction":3,"thesis":"no dte"},'
     '{"underlying":"NVDA","is_index":false,"direction":"bullish","structure":"long call","target_dte":10,"target_delta":0.4,"est_debit_usd":70,"conviction":4,"thesis":"ok"}'
     ']}'),
    '{"trades":[{"underlying":"IWM","direction":"bullish","structure":"long call","target_dte":7,"target_delta":0.3,"est_debit_usd":40,"conviction":3,"thesis":"x"}]}',
    ('{"trades":[{"underlying":"IWM","is_index":true,"direction":"bullish","structure":"long call",'
     '"target_dte":20,"target_delta":0.4,"est_debit_usd":600,"conviction":7,"profit_target_pct":75,'
     '"stop_pct":40,"thesis":"x"}]}'),
    # per-share debit repaired by normalize_debit (x100)
    '{"trades":[{"underlying":"AAPL","direction":"bullish","structure":"long call","target_dte":30,"target_delta":0.4,"est_debit_usd":1.85,"conviction":6,"thesis":"per-share"}]}',
    # the :201 rejections
    '{"trades":[{"underlying":"AAPL","direction":"bullish","structure":"long call","target_dte":30,"target_delta":0.4,"est_debit_usd":0,"conviction":6,"thesis":"zero debit"}]}',
    '{"trades":[{"underlying":"AAPL","direction":"bullish","structure":"long call","target_dte":0,"target_delta":0.4,"est_debit_usd":300,"conviction":6,"thesis":"zero dte"}]}',
    '{"trades":[{"underlying":"AAPL","direction":"bullish","structure":"long call","target_dte":30,"target_delta":0,"est_debit_usd":300,"conviction":6,"thesis":"zero delta"}]}',
    # debit spread + hold window
    '{"trades":[{"underlying":"MSFT","is_index":false,"direction":"bullish","structure":"call debit spread","target_dte":60,"target_delta":0.45,"est_debit_usd":250,"conviction":8,"intended_hold_days":21,"thesis":"spread"}]}',
]

# Frozen from the pre-change parser (git 02298d8d...). Only the *pre-existing* fields are
# compared here; the new credit fields are asserted separately to be at their defaults.
LEGACY_FIELDS = ["underlying", "is_index", "direction", "structure", "target_dte", "target_delta",
                 "est_debit_usd", "conviction", "thesis", "profit_target_pct", "stop_pct",
                 "intended_hold_days"]

GOLDEN = [
    [("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend up", 0.0, 0.0, None)],
    [("QQQ", True, "bearish", "long put", 5, 0.3, 80.0, 3, "weak", 0.0, 0.0, None)],
    [],
    [],
    [],
    [("SPY", True, "bullish", "long call", 7, 1.0, 50.0, 9, "clamp", 0.0, 0.0, None)],
    [("NVDA", False, "bullish", "long call", 10, 0.4, 70.0, 4, "ok", 0.0, 0.0, None)],
    [("IWM", True, "bullish", "long call", 7, 0.3, 40.0, 3, "x", 0.0, 0.0, None)],
    [("IWM", True, "bullish", "long call", 20, 0.4, 600.0, 7, "x", 75.0, 40.0, None)],
    [("AAPL", False, "bullish", "long call", 30, 0.4, 185.0, 6, "per-share", 0.0, 0.0, None)],
    [],
    [],
    [],
    [("MSFT", False, "bullish", "call debit spread", 60, 0.45, 250.0, 8, "spread", 0.0, 0.0, 21)],
]


def _legacy_tuple(idea):
    return tuple(getattr(idea, f) for f in LEGACY_FIELDS)


@pytest.mark.parametrize("raw,expected", list(zip(DEBIT_CORPUS, GOLDEN)))
def test_debit_payloads_parse_to_the_frozen_golden_result(raw, expected):
    got = [_legacy_tuple(i) for i in parse_ideas(raw)]
    assert got == [tuple(e) for e in expected]


def test_debit_ideas_leave_all_credit_fields_at_defaults():
    for raw in DEBIT_CORPUS:
        for i in parse_ideas(raw):
            assert (i.side, i.collateral_usd, i.net_credit_usd, i.max_loss_usd, i.strike) == \
                ("debit", 0.0, 0.0, 0.0, 0.0), "debit idea acquired credit state: %r" % (i,)


def test_negative_control_golden_snapshot_catches_a_parser_change(monkeypatch):
    """NEGATIVE CONTROL: break normalize_debit and the golden comparison must go red."""
    monkeypatch.setattr(strategist, "normalize_debit", lambda v: v)
    got = [_legacy_tuple(i)
           for i in parse_ideas(DEBIT_CORPUS[9])]        # the 1.85 per-share payload
    assert got != [tuple(e) for e in GOLDEN[9]], (
        "negative control failed: the golden snapshot did not notice normalize_debit being "
        "disabled, so it cannot be guarding the debit path")


# --------------------------------------------------------------------------------------------
# 9. THE DEBIT PATH IS UNCHANGED -- differential against the pre-change parser from git
# --------------------------------------------------------------------------------------------

def _load_pre_change_strategist():
    """Materialise the committed (pre-credit) strategist.py as a throwaway module."""
    try:
        src = subprocess.run(
            ["git", "show", "HEAD:exitmgr/strategist.py"],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:      # pragma: no cover
        pytest.skip("git unavailable: %s" % exc)
    if src.returncode != 0 or not src.stdout.strip():         # pragma: no cover
        pytest.skip("could not read HEAD:exitmgr/strategist.py")
    mod = types.ModuleType("_strategist_pre_credit")
    mod.__file__ = "<git HEAD:exitmgr/strategist.py>"
    exec(compile(src.stdout, mod.__file__, "exec"), mod.__dict__)
    return mod


def test_debit_payloads_parse_identically_to_the_pre_change_parser():
    """The strongest form of 'the debit path is unchanged': run every debit payload through the
    committed parser and the current one, and demand identical output."""
    old = _load_pre_change_strategist()
    if any(f.name == "side" for f in dataclasses.fields(old.TradeIdea)):   # pragma: no cover
        pytest.skip("HEAD already contains the credit limb; differential is moot")
    for raw in DEBIT_CORPUS:
        before = [tuple(getattr(i, f) for f in LEGACY_FIELDS) for i in old.parse_ideas(raw)]
        after = [_legacy_tuple(i) for i in parse_ideas(raw)]
        assert after == before, "debit behaviour changed for payload: %s" % raw[:90]


def test_pre_change_parser_cannot_express_a_csp():
    """Proves the differential above is meaningful AND documents why this work exists: the
    committed parser drops a cash-secured put entirely (est_debit_usd missing -> KeyError)."""
    old = _load_pre_change_strategist()
    if any(f.name == "side" for f in dataclasses.fields(old.TradeIdea)):   # pragma: no cover
        pytest.skip("HEAD already contains the credit limb")
    assert old.parse_ideas(wrap(csp())) == []
    assert len(parse_ideas(wrap(csp()))) == 1


def test_negative_control_differential_detects_a_debit_change(monkeypatch):
    """NEGATIVE CONTROL: with normalize_debit disabled the differential must report a change."""
    old = _load_pre_change_strategist()
    monkeypatch.setattr(strategist, "normalize_debit", lambda v: v)
    raw = DEBIT_CORPUS[9]
    before = [tuple(getattr(i, f) for f in LEGACY_FIELDS) for i in old.parse_ideas(raw)]
    after = [_legacy_tuple(i) for i in parse_ideas(raw)]
    assert after != before, (
        "negative control failed: the differential cannot see a deliberate debit-path change")


# --------------------------------------------------------------------------------------------
# 10. The dataclass itself stays backward compatible
# --------------------------------------------------------------------------------------------

def test_legacy_positional_construction_still_works():
    """Existing call sites build TradeIdea positionally with 9 args (tests/test_trader.py:13 and
    friends). The new fields must be trailing and defaulted."""
    i = TradeIdea("SPY", True, "bullish", "long call", 7, 0.35, 90.0, 4, "trend")
    assert i.side == "debit"
    assert (i.collateral_usd, i.net_credit_usd, i.max_loss_usd, i.strike) == (0.0, 0.0, 0.0, 0.0)


def test_new_fields_are_all_trailing():
    names = [f.name for f in dataclasses.fields(TradeIdea)]
    legacy = ["underlying", "is_index", "direction", "structure", "target_dte", "target_delta",
              "est_debit_usd", "conviction", "thesis", "profit_target_pct", "stop_pct",
              "intended_hold_days"]
    assert names[:len(legacy)] == legacy, "the legacy field order must not move"
    assert names[len(legacy):] == ["side", "collateral_usd", "net_credit_usd", "max_loss_usd",
                                   "strike"]


def test_asdict_round_trip_includes_the_credit_state():
    """Journalling/capture serialises ideas with dataclasses.asdict; the exit manager needs the
    credit state to reason about the position (spec section 2)."""
    d = dataclasses.asdict(parse_ideas(wrap(csp()))[0])
    assert d["side"] == "credit" and d["collateral_usd"] == 12000.0
    assert d["net_credit_usd"] == 185.0 and d["max_loss_usd"] == 11815.0 and d["strike"] == 120.0


# ============================================================================================
# AUDIT R4 (2026-07-26) -- the four reproduced contract holes and the S4 direction ruling.
#
# Every test below uses the EXACT payload that reproduced the hole on strategist.py
# d54b571c4543da5d7930cc5a0cf90b4c7e420de8e01d7c93a31654018be52237 (the reproduction script
# was /tmp/verify_r4.py). Mutation results for these tests: tests/MUTANTS_credit_contract.md.
# ============================================================================================

def r4(**over):
    """The exact base payload from the R4 reproduction script, including its stray
    `contracts` key (which TradeIdea has no field for) and `profit_target_pct: None`."""
    t = {"underlying": "NVDA", "is_index": False, "target_dte": 400, "target_delta": 0.6,
         "conviction": 6, "thesis": "x", "stop_pct": 30, "profit_target_pct": None,
         "contracts": 1}
    t.update(over)
    return t


# --------------------------------------------------------------------------------------------
# 11. HOLE 1 -- A NAKED CALL WAS EXPRESSIBLE ON THE DEBIT PATH
#
# The structure gate lived only inside the credit branch, so a short structure carrying a
# positive est_debit_usd sailed straight through the path that had no gate at all.
# --------------------------------------------------------------------------------------------

def test_hole1_naked_call_on_the_debit_path_is_rejected():
    """THE EXACT R4 REPRODUCTION: side absent, structure 'naked call', $500 debit.
    Before the fix this returned 1 idea with structure='naked call', side='debit'."""
    t = r4(structure="naked call", est_debit_usd=500.0, direction="bullish")
    assert "side" not in t, "the reproduction has NO side key -- that is the point"
    assert parse_ideas(wrap(t)) == [], (
        "an unbounded-loss structure must be UNPARSEABLE on every path, not just the credit "
        "one -- this exact payload parsed cleanly on d54b571c")


def test_hole1_short_strangle_with_explicit_debit_side_is_rejected():
    """THE EXACT R4 REPRODUCTION #2: side='debit' spelled out, structure 'short strangle'."""
    t = r4(side="debit", structure="short strangle", est_debit_usd=500.0, direction="bullish")
    assert parse_ideas(wrap(t)) == []


def test_hole1_naked_call_still_rejected_on_the_credit_path():
    """The gate that already worked must keep working (no regression)."""
    t = r4(side="credit", structure="naked call", strike=100.0, collateral_usd=10000.0,
           net_credit_usd=200.0, max_loss_usd=9800.0, direction="bullish")
    assert parse_ideas(wrap(t)) == []


# The full short/undefined vocabulary, now refused on the DEBIT path with a perfect debit.
DEBIT_PATH_BANNED = [
    "naked call", "naked put", "short call", "short put", "sell call", "sell to open call",
    "covered call", "short strangle", "strangle", "short straddle", "straddle",
    "iron condor", "iron butterfly", "ratio spread", "calendar spread", "diagonal spread",
    "credit spread", "call credit spread", "put credit spread", "bull put spread",
    "bear call spread", "cash secured put", "csp", "x", "", "options trade", "spread",
]


@pytest.mark.parametrize("structure", DEBIT_PATH_BANNED)
@pytest.mark.parametrize("side", [None, "debit", "DEBIT", " Debit "])
def test_hole1_allowlist_is_enforced_on_every_debit_spelling(structure, side):
    """Nothing else is wrong with these ideas: positive debit, valid dte/delta/conviction,
    explicit direction. The ONLY possible reason for the drop is the structure allow-list."""
    t = debit(structure=structure, direction="bullish")
    if side is not None:
        t["side"] = side
    assert parse_ideas(wrap(t)) == [], (
        "structure %r on side=%r must be refused by the allow-list" % (structure, side))


@pytest.mark.parametrize("structure", sorted(strategist.DEBIT_STRUCTURES))
def test_hole1_every_allowlisted_debit_structure_still_parses(structure):
    """The allow-list must not be so tight that it silently kills the live slate. Each
    permitted structure parses, and the ORIGINAL string is stored (not a canonical rewrite)."""
    direction = "bearish" if "put" in structure else "bullish"
    ideas = parse_ideas(wrap(debit(structure=structure, direction=direction)))
    assert len(ideas) == 1, "permitted structure %r must parse" % structure
    assert ideas[0].structure == structure, "the allow-list must gate, never rewrite"


@pytest.mark.parametrize("raw,canon", [("Long Call", "long call"),
                                       ("  LONG   PUT  ", "long put"),
                                       ("Call\tDebit\nSpread", "call debit spread")])
def test_hole1_allowlist_matches_case_and_whitespace_variants_but_stores_the_original(raw, canon):
    ideas = parse_ideas(wrap(debit(structure=raw, direction="bullish")))
    assert len(ideas) == 1 and ideas[0].structure == raw.strip(), (
        "membership is tested on the canonical form %r, but the stored structure must remain "
        "the model's own string" % canon)


def test_negative_control_hole1_the_allowlist_is_what_rejects_the_naked_call(monkeypatch):
    """NEGATIVE CONTROL. Neuter ONLY the allow-list and the naked call must sail through
    again -- otherwise the tests above are green for some unrelated reason."""
    monkeypatch.setattr(strategist, "_require_allowed_structure", lambda side, raw: "")
    ideas = parse_ideas(wrap(r4(structure="naked call", est_debit_usd=500.0,
                                direction="bullish")))
    assert len(ideas) == 1 and ideas[0].structure == "naked call", (
        "negative control failed: with the allow-list disabled the naked call should parse")


# The exhaustive record of what the allow-list newly refuses that the OLD parser accepted.
# Each entry is (payload, why it is genuinely unsafe or unexecutable). See section 16 for the
# differential that proves this list is complete over a 19,200-case grid.
NEWLY_BANNED_BY_ALLOWLIST = [
    ('{"trades":[{"underlying":"SPY","direction":"bullish","structure":"x","target_dte":7,'
     '"target_delta":1.7,"est_debit_usd":50,"conviction":9,"thesis":"clamp"},'
     '{"underlying":"SPY","direction":"sideways","structure":"iron condor","target_dte":7,'
     '"target_delta":0.3,"est_debit_usd":50,"conviction":3,"thesis":"drop-dir"},'
     '{"underlying":"SPY","direction":"bullish","structure":"x","target_dte":7,'
     '"target_delta":0.3,"est_debit_usd":-5,"conviction":3,"thesis":"drop-debit"}]}',
     'placeholder structure "x": undefined risk profile. trader.py:1926 would have built a '
     'single long call from it purely because the string lacks "spread" -- i.e. it executes '
     'something the model never actually specified. Fail closed.'),
]


@pytest.mark.parametrize("raw,why", NEWLY_BANNED_BY_ALLOWLIST)
def test_newly_banned_payloads_were_accepted_before_and_are_refused_now(raw, why):
    """Documents -- and pins -- every place the allow-list narrows the debit path."""
    old = _load_pre_change_strategist()
    assert old.parse_ideas(raw), "this payload is only interesting if the OLD parser took it"
    assert parse_ideas(raw) == [], why


# --------------------------------------------------------------------------------------------
# 12. HOLE 2 -- COLLATERAL WAS NOT BOUND TO THE STRIKE
# --------------------------------------------------------------------------------------------

def test_hole2_collateral_of_one_dollar_against_a_120_strike_is_rejected():
    """THE EXACT R4 REPRODUCTION: strike=120, collateral_usd=1.00, net_credit_usd=0.50,
    max_loss_usd=0.50. Every old gate passed -- all four fields positive, and the max_loss
    arithmetic agreed *because it was computed from the same fictional collateral*. A $12,000
    assignment obligation was declared as a $1 one."""
    t = r4(side="credit", structure="cash secured put", strike=120.0, collateral_usd=1.0,
           net_credit_usd=0.50, max_loss_usd=0.50, direction="bullish")
    assert abs(t["max_loss_usd"] - (t["collateral_usd"] - t["net_credit_usd"])) < 0.01, (
        "sanity: the OLD arithmetic gate is satisfied, so it cannot be the reason for the drop")
    assert parse_ideas(wrap(t)) == []


@pytest.mark.parametrize("strike,collateral", [
    (120.0, 1.0),          # the reproduction
    (120.0, 6000.0),       # exactly half a contract
    (120.0, 11999.0),      # a dollar short of one contract
    (120.0, 12001.0),      # a dollar over one contract
    (120.0, 18000.0),      # 1.5 contracts
    (120.0, 120.0),        # forgot the x100 multiplier
    (100.0, 12000.0),      # collateral of a different strike entirely
])
def test_hole2_collateral_must_be_a_whole_multiple_of_strike_times_100(strike, collateral):
    t = csp(strike=strike, collateral_usd=collateral, net_credit_usd=100.0,
            max_loss_usd=collateral - 100.0)
    assert parse_ideas(wrap(t)) == [], (
        "collateral $%.2f is not a whole multiple of strike %.2f x 100" % (collateral, strike))


@pytest.mark.parametrize("strike,contracts", [(120.0, 1), (120.0, 2), (120.0, 7),
                                              (5.0, 1), (12.0, 3), (122.5, 1), (7.5, 4),
                                              (187.5, 2)])
def test_hole2_a_whole_number_of_contracts_is_accepted(strike, contracts):
    collateral = strike * 100.0 * contracts
    credit = round(collateral * 0.015, 2)
    t = csp(strike=strike, collateral_usd=collateral, net_credit_usd=credit,
            max_loss_usd=round(collateral - credit, 2))
    ideas = parse_ideas(wrap(t))
    assert len(ideas) == 1, "%d x %.2f CSP must parse (collateral $%.2f)" % (
        contracts, strike, collateral)
    assert ideas[0].collateral_usd == collateral


def test_hole2_implied_contract_count_is_computed_and_exposed():
    """The parser cannot verify the contract COUNT (TradeIdea has no `contracts` field), but
    it does compute what the declared collateral implies. RESIDUAL: the submit path must
    re-verify collateral == strike * 100 * order.totalQuantity against the real order."""
    out = strategist._parse_credit_fields(csp(strike=120.0, collateral_usd=36000.0,
                                              net_credit_usd=500.0, max_loss_usd=35500.0))
    assert out["implied_contracts"] == 3


def test_negative_control_hole2_the_collateral_binding_is_what_rejects_it(monkeypatch):
    """NEGATIVE CONTROL: disable ONLY the strike binding and the $1 collateral parses again."""
    monkeypatch.setattr(strategist, "_implied_contracts", lambda strike_c, coll_c: 1)
    t = r4(side="credit", structure="cash secured put", strike=120.0, collateral_usd=1.0,
           net_credit_usd=0.50, max_loss_usd=0.50, direction="bullish")
    ideas = parse_ideas(wrap(t))
    assert len(ideas) == 1 and ideas[0].collateral_usd == 1.0, (
        "negative control failed: the collateral tests are not exercising the strike binding")


# --------------------------------------------------------------------------------------------
# 13. HOLE 3 -- THE CREDIT COULD EXCEED THE COLLATERAL
# --------------------------------------------------------------------------------------------

def test_hole3_credit_exceeding_collateral_is_rejected():
    """THE EXACT R4 REPRODUCTION: collateral_usd=100, net_credit_usd=100.005,
    max_loss_usd=0.001. Economic loss is NEGATIVE; the old absolute-difference tolerance
    masked it because |0.001 - (100 - 100.005)| = 0.006 < 0.01."""
    t = r4(side="credit", structure="cash secured put", strike=1.0, collateral_usd=100.0,
           net_credit_usd=100.005, max_loss_usd=0.001, direction="bullish")
    assert abs(t["max_loss_usd"] - (t["collateral_usd"] - t["net_credit_usd"])) < 0.01, (
        "sanity: the OLD tolerance is satisfied -- that is exactly how this got through")
    assert parse_ideas(wrap(t)) == []


@pytest.mark.parametrize("collateral,credit", [
    (12000.0, 12000.0),    # exactly equal -- zero max loss, still impossible
    (12000.0, 12000.01),   # a cent over
    (12000.0, 24000.0),    # nonsense
    (100.0, 100.005),      # the reproduction
])
def test_hole3_collateral_must_strictly_exceed_the_credit(collateral, credit):
    t = csp(strike=collateral / 100.0, collateral_usd=collateral, net_credit_usd=credit,
            max_loss_usd=max(0.01, round(collateral - credit, 2)))
    assert parse_ideas(wrap(t)) == []


def test_hole3_a_credit_one_cent_below_collateral_is_still_structurally_valid():
    """The gate is `collateral > credit`, strictly -- it must not over-reject."""
    t = csp(strike=120.0, collateral_usd=12000.0, net_credit_usd=11999.99, max_loss_usd=0.01)
    assert len(parse_ideas(wrap(t))) == 1


def test_negative_control_hole3_the_credit_lt_collateral_gate_is_live(monkeypatch):
    """NEGATIVE CONTROL: with the strike binding relaxed AND this gate removed, the payload
    parses -- proving it is this gate, not hole 2's, doing the rejecting."""
    monkeypatch.setattr(strategist, "_implied_contracts", lambda strike_c, coll_c: 1)
    real = strategist._parse_credit_fields

    def no_credit_gate(t):
        t2 = dict(t)
        t2["net_credit_usd"] = float(t["collateral_usd"]) / 2.0
        t2["max_loss_usd"] = float(t["collateral_usd"]) - t2["net_credit_usd"]
        out = real(t2)
        out["net_credit_usd"] = float(t["net_credit_usd"])
        out["max_loss_usd"] = float(t["max_loss_usd"])
        return out

    monkeypatch.setattr(strategist, "_parse_credit_fields", no_credit_gate)
    t = r4(side="credit", structure="cash secured put", strike=1.0, collateral_usd=100.0,
           net_credit_usd=100.005, max_loss_usd=0.001, direction="bullish")
    ideas = parse_ideas(wrap(t))
    assert len(ideas) == 1 and ideas[0].net_credit_usd == 100.005, (
        "negative control failed: the credit-vs-collateral tests prove nothing")


# --------------------------------------------------------------------------------------------
# 14. HOLE 4 -- FLOAT-FRAGILE CENT ARITHMETIC, AND BOOLEANS AS NUMBERS
# --------------------------------------------------------------------------------------------

def test_hole4_the_nominal_exact_one_cent_boundary_is_inclusive_and_accepted():
    """THE EXACT R4 REPRODUCTION: max_loss off by a nominal EXACTLY $0.01. In binary floats
    abs(11815.01 - (12000.0 - 185.0)) == 0.010000000000218279 > 0.01, so the spec's own
    stated boundary was REJECTED while $0.009 passed. DECLARED BOUNDARY: inclusive."""
    assert abs(11815.01 - (12000.0 - 185.0)) > 0.01, (
        "sanity: this is the binary-float artefact being fixed")
    for ml in (11815.01, 11814.99):
        ideas = parse_ideas(wrap(csp(max_loss_usd=ml)))
        assert len(ideas) == 1, "the inclusive $0.01 boundary must ACCEPT max_loss=%r" % ml


def test_hole4_two_cents_is_still_outside_the_declared_boundary():
    for ml in (11815.02, 11814.98):
        assert parse_ideas(wrap(csp(max_loss_usd=ml))) == []


def test_hole4_sub_cent_noise_does_not_change_the_verdict():
    """$0.009 passed before and still passes; it is now inside the boundary by construction
    rather than by accident of representation."""
    assert len(parse_ideas(wrap(csp(max_loss_usd=11815.009)))) == 1


NUMERIC_FIELDS_DEBIT = ["est_debit_usd", "target_dte", "target_delta", "conviction"]
NUMERIC_FIELDS_CREDIT = ["strike", "collateral_usd", "net_credit_usd", "max_loss_usd",
                         "target_dte", "target_delta", "conviction"]


@pytest.mark.parametrize("field", NUMERIC_FIELDS_DEBIT)
@pytest.mark.parametrize("value", [True, False])
def test_hole4_boolean_in_a_debit_numeric_field_is_rejected(field, value):
    """`float(True) == 1.0`, so `"est_debit_usd": true` used to become a $100 debit (via
    normalize_debit's x100 rescale) and `"conviction": true` a conviction of 1. A boolean is
    not a number and must never be coerced into one on a real-money path."""
    assert parse_ideas(wrap(debit(**{field: value}))) == [], (
        "debit idea with %s=%r must be dropped, not coerced" % (field, value))


@pytest.mark.parametrize("field", NUMERIC_FIELDS_CREDIT)
@pytest.mark.parametrize("value", [True, False])
def test_hole4_boolean_in_a_credit_numeric_field_is_rejected(field, value):
    assert parse_ideas(wrap(csp(**{field: value}))) == []


def test_hole4_the_exact_boolean_coercion_the_old_parser_performed():
    """Pins WHAT the old behaviour was, so the fix is not mistaken for a no-op: the committed
    parser turned `est_debit_usd: true` into a $100 debit."""
    old = _load_pre_change_strategist()
    raw = wrap(debit(est_debit_usd=True))
    was = old.parse_ideas(raw)
    assert len(was) == 1 and was[0].est_debit_usd == 100.0, (
        "the old parser fabricated a $100 debit from a JSON boolean")
    assert parse_ideas(raw) == []


def test_hole4_booleans_in_the_clamped_pct_fields_fall_back_to_the_default_rule():
    """profit_target_pct/stop_pct never drop an idea; a boolean there must behave exactly like
    an omitted field (0.0 == 'use the default rule'), not clamp up to the 20%/10% floor."""
    i = parse_ideas(wrap(debit(profit_target_pct=True, stop_pct=True)))[0]
    assert (i.profit_target_pct, i.stop_pct) == (0.0, 0.0)
    assert strategist._clamp_pct(True, 20.0, 500.0) == 0.0
    assert strategist._clamp_pct(False, 10.0, 90.0) == 0.0


def test_hole4_is_index_is_still_allowed_to_be_a_boolean():
    """is_index is a genuine boolean field -- the bool rejection must not leak into it."""
    assert parse_ideas(wrap(debit(is_index=True)))[0].is_index is True
    assert parse_ideas(wrap(debit(underlying="NVDA", is_index=False)))[0].is_index is False


def test_hole4_cents_helper_is_exact_and_fails_closed():
    assert strategist._cents(0.01, "x") == 1
    assert strategist._cents(11815.01, "x") == 1181501
    assert strategist._cents("12000.00", "x") == 1200000
    assert strategist._cents(100.005, "x") == 10001          # ROUND_HALF_UP, declared
    for bad in (True, False, None, "", "abc", [], {}, float("nan"), float("inf"),
                float("-inf")):
        with pytest.raises(ValueError):
            strategist._cents(bad, "x")
    # The non-finite refusal must be EXPLICIT and attributable, not an incidental
    # int(Decimal('NaN')) blow-up: Decimal('NaN').quantize() returns NaN rather than raising,
    # so without the is_finite guard a NaN travels one step further before dying with an
    # unrelated message. A real-money audit log has to say WHY the idea was dropped.
    for bad in (float("nan"), float("inf"), float("-inf"), "NaN", "Infinity"):
        with pytest.raises(ValueError, match="must be finite"):
            strategist._cents(bad, "collateral_usd")


def test_negative_control_hole4_float_tolerance_would_reject_the_exact_cent(monkeypatch):
    """NEGATIVE CONTROL: restore the float comparison and the exact $0.01 boundary goes red
    again -- proving the acceptance test above is measuring the cent-safe arithmetic."""
    real = strategist._parse_credit_fields

    def floaty(t):
        out = real(dict(t, max_loss_usd=float(t["collateral_usd"]) - float(t["net_credit_usd"])))
        ml, coll, cr = (float(t["max_loss_usd"]), float(t["collateral_usd"]),
                        float(t["net_credit_usd"]))
        if abs(ml - (coll - cr)) > 0.01:          # the old, binary-float gate
            raise ValueError("float tolerance")
        out["max_loss_usd"] = ml
        return out

    monkeypatch.setattr(strategist, "_parse_credit_fields", floaty)
    assert parse_ideas(wrap(csp(max_loss_usd=11815.01))) == [], (
        "negative control failed: the old float gate no longer rejects the exact cent, so the "
        "cent-safe test above cannot be detecting the fix")


# --------------------------------------------------------------------------------------------
# 15. S4 RULING -- A CASH-SECURED PUT IS BULLISH (credit-scoped carve-out)
# --------------------------------------------------------------------------------------------

def test_s4_omitted_direction_on_a_valid_csp_infers_bullish():
    """THE EXACT R4 REPRODUCTION: a valid CSP with NO direction key inferred 'bearish',
    because normalize_direction saw 'put' in the structure string."""
    t = r4(side="credit", structure="cash secured put", strike=100.0, collateral_usd=10000.0,
           net_credit_usd=150.0, max_loss_usd=9850.0)
    assert "direction" not in t
    ideas = parse_ideas(wrap(t))
    assert len(ideas) == 1 and ideas[0].direction == "bullish", (
        "a short put has POSITIVE delta -- it is a bullish/neutral thesis")


@pytest.mark.parametrize("raw_dir", ["", None, "sideways", "neutral", "flat", [], {}, 7])
def test_s4_blank_or_unrecognised_direction_on_a_csp_infers_bullish(raw_dir):
    ideas = parse_ideas(wrap(csp(direction=raw_dir)))
    assert len(ideas) == 1 and ideas[0].direction == "bullish"


@pytest.mark.parametrize("raw_dir", ["bullish", "bull", "long", "up", "BULLISH", " Bullish "])
def test_s4_explicit_bullish_on_a_csp_is_accepted(raw_dir):
    ideas = parse_ideas(wrap(csp(direction=raw_dir)))
    assert len(ideas) == 1 and ideas[0].direction == "bullish"


@pytest.mark.parametrize("raw_dir", ["bearish", "bear", "short", "down", "put", "puts",
                                     "sell", "downtrend", "BEARISH", " Bearish "])
def test_s4_explicit_bearish_on_a_csp_is_rejected_not_corrected(raw_dir):
    """FAIL CLOSED. A model that asks to sell a put while calling the position bearish does
    not understand what it is proposing; silently rewriting the direction would hide that."""
    assert parse_ideas(wrap(csp(direction=raw_dir))) == [], (
        "explicit %r on a cash-secured put must be rejected as inconsistent" % raw_dir)


def test_s4_the_generic_put_mapping_is_untouched_an_ordinary_long_put_is_bearish():
    """THE BOUNDARY OF THE CARVE-OUT. This is the regression that would matter most."""
    i = parse_ideas(wrap(debit(structure="long put", direction="bearish",
                               underlying="QQQ")))[0]
    assert i.direction == "bearish"
    # and with the direction OMITTED, the generic structure inference still says bearish
    t = debit(structure="long put", underlying="QQQ")
    del t["direction"]
    assert parse_ideas(wrap(t))[0].direction == "bearish"
    t = debit(structure="put debit spread", underlying="QQQ")
    del t["direction"]
    assert parse_ideas(wrap(t))[0].direction == "bearish"
    # the mapping table itself is unchanged
    assert strategist.normalize_direction("", "long put") == "bearish"
    assert strategist.normalize_direction("put", "long call") == "bearish"


def test_s4_carve_out_is_unreachable_for_a_structure_that_is_not_a_valid_csp():
    """normalize_csp_direction only ever runs AFTER _parse_credit_fields has proved the idea
    is a fully specified cash-secured put. A broken CSP is dropped before direction matters."""
    assert parse_ideas(wrap(csp(collateral_usd=1.0, direction="bullish"))) == []
    assert parse_ideas(wrap(csp(structure="short put", direction="bullish"))) == []


def test_s4_normalize_csp_direction_unit_contract():
    assert strategist.normalize_csp_direction("") == "bullish"
    assert strategist.normalize_csp_direction(None) == "bullish"
    assert strategist.normalize_csp_direction("bullish") == "bullish"
    assert strategist.normalize_csp_direction("call") == "bullish"
    with pytest.raises(ValueError):
        strategist.normalize_csp_direction("bearish")
    with pytest.raises(ValueError):
        strategist.normalize_csp_direction("put")


def test_negative_control_s4_the_carve_out_is_what_produces_bullish(monkeypatch):
    """NEGATIVE CONTROL: revert to the generic mapping and the CSP goes bearish again."""
    monkeypatch.setattr(strategist, "normalize_csp_direction",
                        lambda raw: strategist.normalize_direction(raw, CSP_STRUCTURE) or "")
    t = r4(side="credit", structure="cash secured put", strike=100.0, collateral_usd=10000.0,
           net_credit_usd=150.0, max_loss_usd=9850.0)
    ideas = parse_ideas(wrap(t))
    assert len(ideas) == 1 and ideas[0].direction == "bearish", (
        "negative control failed: the S4 tests are not exercising the carve-out")


# --------------------------------------------------------------------------------------------
# 16. THE 19,200-CASE OLD/NEW DEBIT DIFFERENTIAL
#
# The non-negotiable of the R4 remediation: the debit path's accept/reject set must be
# IDENTICAL to the committed pre-change parser for every payload that is not a newly-banned
# structure. This runs the cross product against `git show HEAD:exitmgr/strategist.py` and
# buckets every difference by cause.
# --------------------------------------------------------------------------------------------

DIFF_ALLOWED_STRUCTURES = ["long call", "long put", "call debit spread", "put debit spread",
                           "bull call spread", "bear put spread", "debit spread", "long option"]
DIFF_BANNED_STRUCTURES = ["naked call", "short call", "short strangle", "iron condor",
                          "credit spread", "bull put spread", "x", ""]
DIFF_DIRECTIONS = ["bullish", "bearish", "call", "put", "sideways"]
DIFF_DEBITS = [0, -5, 1.85, 50, 420, 12000]
DIFF_DTES = [0, 7, 45, 400]
DIFF_DELTAS = [0, 0.35, 1.7, -0.4, 1.0]
DIFF_CONVICTIONS = [3, 99]


def _differential_grid():
    for structure in DIFF_ALLOWED_STRUCTURES + DIFF_BANNED_STRUCTURES:
        for direction in DIFF_DIRECTIONS:
            for est in DIFF_DEBITS:
                for dte in DIFF_DTES:
                    for delta in DIFF_DELTAS:
                        for conv in DIFF_CONVICTIONS:
                            yield structure, wrap({
                                "underlying": "SPY", "is_index": True, "direction": direction,
                                "structure": structure, "target_dte": dte,
                                "target_delta": delta, "est_debit_usd": est,
                                "conviction": conv, "thesis": "diff"})


def test_debit_differential_19200_cases_differs_only_on_newly_banned_structures():
    old = _load_pre_change_strategist()
    if any(f.name == "side" for f in dataclasses.fields(old.TradeIdea)):   # pragma: no cover
        pytest.skip("HEAD already contains the credit limb; differential is moot")
    total = accepted_both = 0
    unexplained = []
    banned_diffs = {}
    for structure, raw in _differential_grid():
        total += 1
        before = [tuple(getattr(i, f) for f in LEGACY_FIELDS) for i in old.parse_ideas(raw)]
        after = [_legacy_tuple(i) for i in parse_ideas(raw)]
        if before and after:
            accepted_both += 1
        if before == after:
            continue
        if structure in DIFF_BANNED_STRUCTURES and after == []:
            banned_diffs[structure] = banned_diffs.get(structure, 0) + 1
            continue
        unexplained.append((raw, before, after))
    assert total >= 19200, "the differential must be at least as wide as the R4 run: %d" % total
    assert not unexplained, (
        "%d UNEXPLAINED debit-path differences (first: %r  old=%r  new=%r)"
        % (len(unexplained), unexplained[0][0], unexplained[0][1], unexplained[0][2]))
    # every banned structure that the old parser ever accepted must now show a difference
    assert set(banned_diffs) == {"naked call", "short call", "short strangle", "iron condor",
                                 "credit spread", "bull put spread", "x", ""}, banned_diffs
    assert accepted_both > 0, "a differential where nothing is accepted proves nothing"


def test_debit_differential_allowed_structures_are_bit_identical():
    """The half of the grid that must show ZERO differences, asserted on its own so a
    regression cannot hide inside the banned-structure bucket."""
    old = _load_pre_change_strategist()
    if any(f.name == "side" for f in dataclasses.fields(old.TradeIdea)):   # pragma: no cover
        pytest.skip("HEAD already contains the credit limb; differential is moot")
    n = diffs = 0
    for structure, raw in _differential_grid():
        if structure not in DIFF_ALLOWED_STRUCTURES:
            continue
        n += 1
        before = [tuple(getattr(i, f) for f in LEGACY_FIELDS) for i in old.parse_ideas(raw)]
        after = [_legacy_tuple(i) for i in parse_ideas(raw)]
        if before != after:
            diffs += 1
    assert n == 9600 and diffs == 0, "%d/%d permitted-structure cases changed" % (diffs, n)


# --------------------------------------------------------------------------------------------
# 17. GAPS FOUND BY MUTATION TESTING (see tests/MUTANTS_credit_contract.md)
#
# These exist because mutants M7 and M19 SURVIVED the first run: the sections above rejected
# their payloads through some *other* gate, so removing the real one changed nothing. Each
# test below is the payload that only the named gate rejects.
# --------------------------------------------------------------------------------------------

def test_gap_m7_collateral_of_a_single_cent_implies_zero_contracts():
    """Kills M7 (`if contracts < 1` removed). A collateral inside the one-cent rounding slop
    of zero passes the whole-multiple test with remainder 0 and implies ZERO contracts -- only
    the >= 1 contract floor rejects it."""
    t = csp(strike=120.0, collateral_usd=0.01, net_credit_usd=0.001, max_loss_usd=0.009)
    assert parse_ideas(wrap(t)) == [], (
        "$0.01 of collateral against a $120 strike implies zero contracts")


def test_gap_m19_a_negative_but_self_consistent_net_credit_is_rejected():
    """Kills M19 (`_require_positive`'s positivity gate removed). collateral - (-100) == 12100
    exactly, the collateral is a clean 1x120 multiple, and the credit is below the collateral
    -- every later gate is satisfied. Only the positivity requirement refuses a NEGATIVE
    'credit', which is a debit wearing a credit's name."""
    t = csp(strike=120.0, collateral_usd=12000.0, net_credit_usd=-100.0, max_loss_usd=12100.0)
    assert abs(t["max_loss_usd"] - (t["collateral_usd"] - t["net_credit_usd"])) < 0.01
    assert parse_ideas(wrap(t)) == []


@pytest.mark.parametrize("field,value,partner", [
    ("net_credit_usd", -100.0, {"max_loss_usd": 12100.0}),
    ("max_loss_usd", -11815.0, {}),
    ("strike", -120.0, {}),
    ("collateral_usd", -12000.0, {"max_loss_usd": -12185.0}),
])
def test_gap_m19_negative_credit_fields_are_rejected_even_when_arithmetic_agrees(
        field, value, partner):
    t = csp(**dict({field: value}, **partner))
    assert parse_ideas(wrap(t)) == []
