"""Public API contract; production-derived narrative omitted."""

import inspect
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from exitmgr import construction, risk
from exitmgr import rules as rules_mod
from exitmgr.config import Config, ConstructionConfig, RulesConfig, ScaleOutConfig

REPO = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO / "config.yaml"

C = ConstructionConfig()




PRE_R2_TIERS = [
    {"min_pot": 0,      "tp_max_pct": 0.25, "tp_pct": 0.20},
    {"min_pot": 2500,   "tp_max_pct": 0.30, "tp_pct": 0.22},
    {"min_pot": 5000,   "tp_max_pct": 0.35, "tp_pct": 0.25},
    {"min_pot": 7500,   "tp_max_pct": 0.40, "tp_pct": 0.28},
    {"min_pot": 10000,  "tp_max_pct": 0.45, "tp_pct": 0.32},
    {"min_pot": 25000,  "tp_max_pct": 0.50, "tp_pct": 0.38},
    {"min_pot": 50000,  "tp_max_pct": 0.55, "tp_pct": 0.45},
    {"min_pot": 100000, "tp_max_pct": 0.60, "tp_pct": 0.50},
]

LIVE_NET_LIQ = 1893.0



LEGACY_JOURNAL_TPS = [15.0, 20.0, 40.0, 60.0, 75.0, 100.0]


@pytest.fixture(scope="module")
def shipped_config():
    """Public API contract; production-derived narrative omitted."""
    return Config.from_yaml(str(CONFIG_PATH))


@pytest.fixture(scope="module")
def shipped_yaml():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _cons_for_pot(net_liq, tiers=PRE_R2_TIERS):
    """Public API contract; production-derived narrative omitted."""
    tp_max, tp_def = construction.tp_tier_for_pot(net_liq, tiers, C.tp_max_pct, C.tp_pct)
    return replace(C, tp_max_pct=tp_max, tp_pct=tp_def)



class TestNullSurvivesTheFullPipeline:
    """Public API contract; production-derived narrative omitted."""

    def test_none_stays_none_through_the_construction_clamp(self):
        tp, sl = construction.clamp_tp_sl(None, 30.0, C)
        assert tp is None, "a null take-profit was translated into a number"
        assert sl == 30.0

    def test_absent_and_zero_stay_none(self):
        for absent in (None, 0, 0.0, "", False):
            tp, _sl = construction.clamp_tp_sl(absent, 30.0, C)
            assert tp is None, f"{absent!r} manufactured a take-profit"

    def test_optional_helper_reports_none_without_inventing_a_default(self):
        tp, note = construction.optional_take_profit_pct(None)
        assert tp is None and note == ""

    def test_none_survives_a_journal_round_trip_and_restart(self):
        entry = {"symbol": "X", "profit_target_pct": None, "stop_pct": 30.0,
                 "tp_policy": construction.TP_POLICY_CURRENT}
        reloaded = json.loads(json.dumps(entry))
        assert construction.journal_take_profit_pct(reloaded) is None

    def test_risk_gate_treats_an_absent_target_as_absent(self):
        t = risk.ProposedTrade(underlying="X", notional=500.0, is_index=False, conviction=7)
        assert t.profit_target_pct is None, "ProposedTrade still defaults the target to 0.0"

    def test_the_explicit_pct_reader_never_invents_a_zero(self):
        """Public API contract; production-derived narrative omitted."""
        for absent in (None, "", "nope", float("nan")):
            assert risk._explicit_pct(absent) is None, f"{absent!r} became a number"
        assert risk._explicit_pct(0) == 0.0
        assert risk._explicit_pct(30.0) == 30.0

    def test_risk_gate_does_not_invert_on_a_missing_target(self):
        """Public API contract; production-derived narrative omitted."""
        t = risk.ProposedTrade(underlying="X", notional=100.0, is_index=False, conviction=7,
                               profit_target_pct=None, stop_pct=30.0)
        d = risk.evaluate_trade(t, net_liq=100000.0, available_funds=100000.0,
                                open_positions=[], pot_day_start=100000.0,
                                approved_names={"X"}, limits=risk.RiskLimits())
        assert not any("reward:risk" in r for r in d.reasons)



class TestAccountSizeCannotCreateATarget:
    """Public API contract; production-derived narrative omitted."""

    def test_the_live_pot_stamps_no_target(self):
        cons = _cons_for_pot(LIVE_NET_LIQ)
        tp, sl = construction.clamp_tp_sl(None, 30.0, cons)
        assert tp is None, (
            f"net_liq ${LIVE_NET_LIQ:,.0f} still stamped a +{tp}% target from the floor tier")
        assert sl == 30.0, "the stop moved while removing the tier"

    @pytest.mark.parametrize("net_liq", [0, 1, 999, 1893, 2500, 5000, 7500,
                                         10000, 25000, 50000, 100000, 10_000_000])
    def test_no_pot_size_anywhere_on_the_ladder_creates_a_target(self, net_liq):
        cons = _cons_for_pot(net_liq)
        tp, _sl = construction.clamp_tp_sl(None, 30.0, cons)
        assert tp is None, f"net_liq {net_liq} manufactured +{tp}%"

    def test_a_tiered_cons_cannot_even_promote_a_model_value(self):
        """Public API contract; production-derived narrative omitted."""
        cons = _cons_for_pot(LIVE_NET_LIQ)
        tp, _sl = construction.clamp_tp_sl(20.0, 30.0, cons)
        assert tp is None

    def test_shipped_config_carries_no_tp_tiers(self, shipped_yaml):
        rows = (shipped_yaml.get("caps") or {}).get("tp_tiers")
        assert not rows, f"config.yaml still ships {len(rows or [])} caps.tp_tiers rows"

    def test_shipped_config_object_has_no_tp_tiers(self, shipped_config):
        assert not getattr(shipped_config.caps, "tp_tiers", []), \
            "the loaded CapsConfig still carries tier rows"

    def test_superseded_comment_no_longer_protects_the_mechanism(self):
        """Public API contract; production-derived narrative omitted."""
        text = CONFIG_PATH.read_text()
        assert 'do NOT "fix" this' not in text
        assert "ACCEPTED INVERTED REWARD:RISK ON SMALL POTS" not in text
        assert "RULING_TAKE_PROFIT" in text, "the superseding ruling is not cited in config.yaml"


class TestTheEntryPathItselfCarriesNoTierApplication:
    """Public API contract; production-derived narrative omitted."""

    DR = REPO / "daily_recommend.py"

    def test_the_slate_no_longer_applies_a_pot_tier(self):
        src = self.DR.read_text()
        assert "tp_tier_for_pot(" not in src, \
            "daily_recommend.py applies a pot-tiered take-profit again"
        assert "CAPS_TP_TIERS" not in src, \
            "daily_recommend.py caches caps.tp_tiers again"

    def test_the_slate_stamps_the_migration_flag_on_every_new_journal_line(self):
        src = self.DR.read_text()
        assert '"tp_policy": construction.TP_POLICY_CURRENT' in src, (
            "new journal lines are not stamped with the R2 policy flag -- their take-profit could "
            "not be distinguished from a pre-R2 tier value after a restart")

    def test_the_slate_handles_an_absent_take_profit(self):
        src = self.DR.read_text()
        assert "if tp_pct is not None else None" in src, \
            "the slate assumes a take-profit price always exists"



class TestTwentyPercentAloneDoesNothing:
    """Public API contract; production-derived narrative omitted."""

    def test_global_fallback_is_null(self, shipped_config):
        assert shipped_config.rules.profit_target_pct is None, (
            "rules.profit_target_pct is still a number -- every "
            "`je.get(...) or config.rules.profit_target_pct` read revives a mechanical target")

    def test_global_fallback_is_null_in_raw_yaml(self, shipped_yaml):
        assert (shipped_yaml.get("rules") or {}).get("profit_target_pct") is None

    @pytest.mark.parametrize("gain_pct", [20.0, 25.0, 30.0, 35.0, 50.0, 120.0, 300.0])
    def test_no_winner_is_sold_by_arithmetic(self, shipped_config, gain_pct):
        """Public API contract; production-derived narrative omitted."""
        entry_debit = 1000.0
        qty = 2
        entry_per_share = entry_debit / (100.0 * qty)
        price = entry_per_share * (1 + gain_pct / 100.0)
        trig = rules_mod.evaluate_position(
            con_id=1, symbol="X", quantity=qty, entry_debit=entry_debit,
            current_price=price, days_to_expiry=120, peak_price=None,
            rules=shipped_config.rules)
        assert trig is None, (
            f"a +{gain_pct:g}% winner was mechanically exited via "
            f"{getattr(trig, 'trigger_type', '?')}")

    def test_x_or_fallback_semantics_cannot_resurrect_a_number(self, shipped_config):
        """Public API contract; production-derived narrative omitted."""
        for je in ({}, {"profit_target_pct": None}, {"profit_target_pct": 0}):
            assert (je.get("profit_target_pct") or shipped_config.rules.profit_target_pct) is None



class TestMechanicalScaleOutIsOff:
    """Public API contract; production-derived narrative omitted."""

    def test_shipped_config_disables_scale_out(self, shipped_config):
        assert shipped_config.rules.scale_out.enabled is False, \
            "the +20% mechanical trim is still armed"

    def test_shipped_yaml_disables_scale_out(self, shipped_yaml):
        assert ((shipped_yaml.get("rules") or {}).get("scale_out") or {}).get("enabled") is False

    @pytest.mark.parametrize("gain_pct", [20.0, 21.0, 45.0])
    @pytest.mark.parametrize("qty", [2, 5])
    def test_no_partial_trim_fires(self, shipped_config, gain_pct, qty):
        entry_debit = 100.0 * qty * 5.0
        entry_per_share = entry_debit / (100.0 * qty)
        price = entry_per_share * (1 + gain_pct / 100.0)
        trig = rules_mod.evaluate_position(
            con_id=2, symbol="Y", quantity=qty, entry_debit=entry_debit,
            current_price=price, days_to_expiry=200, peak_price=None,
            rules=shipped_config.rules, already_trimmed=False)
        assert trig is None or trig.trigger_type != "scale_out", \
            "the mechanical scale-out trimmed a runner"



class TestLegacyJournalsCannotReviveTierTargets:
    """Public API contract; production-derived narrative omitted."""

    @pytest.mark.parametrize("legacy_tp", LEGACY_JOURNAL_TPS)
    def test_a_legacy_entry_without_the_policy_flag_yields_no_target(self, legacy_tp):
        je = {"symbol": "IWM", "profit_target_pct": legacy_tp, "stop_pct": 30.0}
        assert construction.journal_take_profit_pct(je) is None, (
            f"a pre-R2 journal line resurrected a mechanical +{legacy_tp}% target")

    def test_a_wrong_or_stale_policy_flag_yields_no_target(self):
        for flag in ("tiered-v1", "", None, "explicit-only-v1", 2):
            je = {"profit_target_pct": 20.0, "tp_policy": flag}
            assert construction.journal_take_profit_pct(je) is None

    def test_the_policy_flag_alone_does_not_admit_a_mechanical_number(self):
        """Public API contract; production-derived narrative omitted."""
        je = {"profit_target_pct": 20.0, "tp_policy": construction.TP_POLICY_CURRENT}
        assert construction.journal_take_profit_pct(je) is None

    def test_a_stamped_explicit_backstop_is_the_only_thing_that_survives(self):
        je = {"profit_target_pct": 300.0, "tp_policy": construction.TP_POLICY_CURRENT}
        assert construction.journal_take_profit_pct(je) == 300.0

    def test_migration_is_not_bypassable_by_a_missing_entry(self):
        for je in (None, {}, {"tp_policy": construction.TP_POLICY_CURRENT}):
            assert construction.journal_take_profit_pct(je) is None



class TestOnlyAnExplicitDistantBackstopIsNumeric:
    """Public API contract; production-derived narrative omitted."""

    @pytest.mark.parametrize("v", [100.0, 150.0, 300.0, 499.9, 500.0])
    def test_explicit_distant_backstop_is_kept(self, v):
        tp, note = construction.optional_take_profit_pct(v)
        assert tp == round(v, 1) and note == ""

    @pytest.mark.parametrize("v", [1.0, 15.0, 20.0, 25.0, 30.0, 35.0, 60.0, 75.0, 99.9, 500.1, 1e9])
    def test_everything_short_of_the_backstop_band_is_refused(self, v):
        tp, note = construction.optional_take_profit_pct(v)
        assert tp is None, f"+{v}% was installed as an automatic exit"


        assert note, "a refused take-profit must be logged explicitly, not silently rewritten"
        assert f"{v:g}" in note, "the refusal does not name the value that was refused"
        assert "NOT installed" in note, "the refusal does not say the target was not installed"
        assert "RULING_TAKE_PROFIT" in note or "R5 R2" in note, \
            "the refusal does not cite the ruling that caused it"
        assert f"{construction.TP_BACKSTOP_MIN_PCT:g}-{construction.TP_BACKSTOP_MAX_PCT:g}" in note, \
            "the refusal does not state the only admissible band"

    @pytest.mark.parametrize("v", ["nonsense", object(), float("nan")])
    def test_garbage_never_becomes_a_target(self, v):
        tp, _note = construction.optional_take_profit_pct(v)
        assert tp is None

    def test_the_band_constants_are_the_ruling_band(self):
        assert construction.TP_BACKSTOP_MIN_PCT == 100.0
        assert construction.TP_BACKSTOP_MAX_PCT == 500.0



class TestLosingBrokenThesisStillBecomesACut:
    """Public API contract; production-derived narrative omitted."""

    @pytest.mark.parametrize("loss_pct", [30.0, 30.1, 45.0, 80.0, 100.0])
    @pytest.mark.parametrize("qty", [1, 2, 5])
    def test_the_stop_still_fires(self, shipped_config, loss_pct, qty):
        entry_debit = 100.0 * qty * 4.0
        entry_per_share = entry_debit / (100.0 * qty)
        price = max(0.01, entry_per_share * (1 - loss_pct / 100.0))
        trig = rules_mod.evaluate_position(
            con_id=3, symbol="Z", quantity=qty, entry_debit=entry_debit,
            current_price=price, days_to_expiry=90, peak_price=None,
            rules=shipped_config.rules)
        assert trig is not None and trig.trigger_type == "stop", (
            f"a -{loss_pct:g}% loser was NOT cut (got {getattr(trig, 'trigger_type', None)})")

    @pytest.mark.parametrize("loss_pct", [1.0, 10.0, 29.0])
    def test_a_shallow_loser_is_not_cut(self, shipped_config, loss_pct):
        entry_debit = 400.0
        entry_per_share = entry_debit / 100.0
        price = entry_per_share * (1 - loss_pct / 100.0)
        trig = rules_mod.evaluate_position(
            con_id=4, symbol="Z", quantity=1, entry_debit=entry_debit,
            current_price=price, days_to_expiry=90, peak_price=None,
            rules=shipped_config.rules)
        assert trig is None









PRE_R2_STOP_SOURCE = [
    "    sl_def = abs(float(cons.sl_pct)) * 100.0",
    "    try:\n        sl_in = float(sl_pct or 0.0)\n    except (TypeError, ValueError):\n        sl_in = 0.0",
    "    sl = sl_def if sl_in <= 0 else min(sl_def, sl_in)",
    "    sl = max(5.0, sl)  # never a hair-trigger stop from a garbage model value",
]


def _pre_r2_stop_reference(sl_pct, cons):
    """Public API contract; production-derived narrative omitted."""
    sl_def = abs(float(cons.sl_pct)) * 100.0
    try:
        sl_in = float(sl_pct or 0.0)
    except (TypeError, ValueError):
        sl_in = 0.0
    sl = sl_def if sl_in <= 0 else min(sl_def, sl_in)
    sl = max(5.0, sl)
    return round(sl, 1)


class TestThirtyPercentStopIsByteForByteUnchanged:

    def test_stop_limb_source_bytes_are_unchanged(self):
        src = inspect.getsource(construction.clamp_stop_pct)
        for block in PRE_R2_STOP_SOURCE:
            assert block in src, f"the stop limb was edited; missing verbatim block:\n{block}"
        assert "return round(sl, 1)" in src

    @pytest.mark.parametrize("sl_in", [None, 0, 0.0, "", -0.0, 1.0, 4.9, 5.0, 5.1, 10.0,
                                       20.0, 29.9, 30.0, 30.1, 50.0, 90.0, 1000.0,
                                       -20.0, "garbage", float("nan"), True, False])
    @pytest.mark.parametrize("sl_cfg", [-0.30, -0.50, -0.05, 0.30])
    def test_stop_behaviour_matches_the_frozen_reference(self, sl_in, sl_cfg):
        cons = replace(C, sl_pct=sl_cfg)
        got = construction.clamp_stop_pct(sl_in, cons)
        want = _pre_r2_stop_reference(sl_in, cons)
        assert got == want or (math.isnan(got) and math.isnan(want)), \
            f"stop clamp changed for sl_in={sl_in!r} sl_cfg={sl_cfg}: {got} != {want}"

    @pytest.mark.parametrize("sl_in", [None, 0.0, 10.0, 30.0, 90.0, "garbage"])
    def test_clamp_tp_sl_stop_limb_still_matches_the_reference(self, sl_in):
        _tp, sl = construction.clamp_tp_sl(None, sl_in, C)
        assert sl == _pre_r2_stop_reference(sl_in, C)

    def test_the_default_stop_magnitude_is_still_thirty(self):
        assert construction.clamp_stop_pct(None, C) == 30.0
        assert construction.clamp_stop_pct(0, C) == 30.0

    def test_a_model_stop_may_tighten_but_never_loosen(self):
        assert construction.clamp_stop_pct(20.0, C) == 20.0
        assert construction.clamp_stop_pct(90.0, C) == 30.0
        assert construction.clamp_stop_pct(1.0, C) == 5.0

    def test_shipped_config_stop_numbers_are_untouched(self, shipped_config, shipped_yaml):
        assert shipped_config.rules.stop_pct == 30.0
        assert (shipped_yaml.get("rules") or {}).get("stop_pct") == 30.0
        assert (shipped_yaml.get("construction") or {}).get("sl_pct") == -0.30

    def test_tiering_still_cannot_reach_the_stop(self):
        stops = {construction.clamp_stop_pct(75.0, _cons_for_pot(nl))
                 for nl in (500, 1893, 6000, 30000, 150000)}
        assert stops == {30.0}
