"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union


CONTRACT_VERSION = "stage-ab.v3"
CONTRACT_SHA256 = "a4118b2e8f4a1f3f8176a514e98102bfb655543dfd1888f4e81918709af37acb"

CONTRACT_SHA = CONTRACT_SHA256



SCHEMA_PATH = Path(__file__).resolve().parents[1] / "STAGE_AB_SCHEMA.frozen.json"

SIDES = frozenset({"debit", "credit"})
DIRECTIONS = frozenset({"bullish", "bearish"})
STRUCTURES = frozenset({
    "long call",
    "long put",
    "call debit spread",
    "put debit spread",
    "cash secured put",
})
LEG_ACTIONS = frozenset({"buy", "sell"})
LEG_RIGHTS = frozenset({"call", "put"})

_UNDERLYING_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,7}$")
_INTENT_ID_RE = re.compile(r"^intent_[1-3]$")
_CANDIDATE_ID_RE = re.compile(r"^cand_[0-9a-f]{64}$")
_EXPIRY_RE = re.compile(r"^[0-9]{8}$")
_MONEY_TOLERANCE = Decimal("0.01")
_FLOAT_TOLERANCE = 1e-9


class EntryContractError(ValueError):
    """Public API contract; production-derived narrative omitted."""


def _duplicate_rejecting_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise EntryContractError("duplicate JSON key: %s" % key)
        out[key] = value
    return out


def _reject_json_constant(value: str) -> None:
    raise EntryContractError("non-finite JSON number is forbidden: %s" % value)


def _check_finite_tree(value: Any, path: str = "$") -> None:
    """Public API contract; production-derived narrative omitted."""
    if isinstance(value, float) and not math.isfinite(value):
        raise EntryContractError("non-finite JSON number at %s" % path)
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_finite_tree(item, "%s[%d]" % (path, index))
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_finite_tree(item, "%s.%s" % (path, key))


def parse_json_document(raw: Union[str, bytes, bytearray]) -> Any:
    """Public API contract; production-derived narrative omitted."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EntryContractError("model output is not valid UTF-8") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise EntryContractError("model output must be str or UTF-8 bytes")
    if not text.strip():
        raise EntryContractError("empty model output")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except EntryContractError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise EntryContractError("output is not one complete JSON document: %s" % exc) from exc
    _check_finite_tree(value)
    return value


def load_frozen_schema(path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Public API contract; production-derived narrative omitted."""
    schema_path = Path(path) if path is not None else SCHEMA_PATH
    try:
        payload = schema_path.read_bytes()
    except OSError as exc:
        raise EntryContractError("cannot read frozen schema %s: %s" % (schema_path, exc)) from exc
    actual = hashlib.sha256(payload).hexdigest()
    if actual != CONTRACT_SHA256:
        raise EntryContractError(
            "frozen schema integrity failure: expected %s, got %s"
            % (CONTRACT_SHA256, actual)
        )
    schema = parse_json_document(payload)
    if not isinstance(schema, dict):
        raise EntryContractError("frozen schema root must be an object")
    if schema.get("contract_version") != CONTRACT_VERSION:
        raise EntryContractError("frozen schema contract_version mismatch")
    if schema.get("status") != "FROZEN":
        raise EntryContractError("schema is not marked FROZEN")
    expected_entrypoints = {
        "stage_a_output": "#/$defs/stage_a_output",
        "stage_b_input": "#/$defs/stage_b_input",
        "stage_b_output": "#/$defs/stage_b_output",
    }
    if schema.get("validation_entrypoints") != expected_entrypoints:
        raise EntryContractError("frozen schema validation entrypoints mismatch")
    return schema




FROZEN_SCHEMA = load_frozen_schema()


@dataclass
class StageAIntent:
    underlying: str
    side: str
    direction: str
    structure: str
    target_dte: int
    intended_hold_days: int
    target_delta: float
    conviction: int
    allocation_pct_net_liq: float
    alpha: str
    thesis: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "underlying": self.underlying,
            "side": self.side,
            "direction": self.direction,
            "structure": self.structure,
            "target_dte": self.target_dte,
            "intended_hold_days": self.intended_hold_days,
            "target_delta": self.target_delta,
            "conviction": self.conviction,
            "allocation_pct_net_liq": self.allocation_pct_net_liq,
            "alpha": self.alpha,
            "thesis": self.thesis,
        }


@dataclass
class RuntimeLeg:
    con_id: int
    action: str
    right: str
    strike: float
    bid_per_share: float
    ask_per_share: float
    delta: float
    iv: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "con_id": self.con_id,
            "action": self.action,
            "right": self.right,
            "strike": self.strike,
            "bid_per_share": self.bid_per_share,
            "ask_per_share": self.ask_per_share,
            "delta": self.delta,
            "iv": self.iv,
        }

    def identity_dict(self) -> Dict[str, Any]:
        return {
            "con_id": self.con_id,
            "action": self.action,
            "right": self.right,
            "strike": float(self.strike),
        }


@dataclass
class RuntimeCandidate:
    candidate_id: str
    intent_id: str
    underlying: str
    side: str
    direction: str
    structure: str
    quote_observed_at_utc: str
    quote_age_seconds: float
    expiry: str
    dte: int
    legs: List[RuntimeLeg]
    combo_bid_per_share: float
    combo_ask_per_share: float
    width_usd: Optional[float]
    primary_leg_delta: float
    primary_leg_iv: float
    bid_ask_spread_pct: float
    liquidity_status: str
    one_contract_cost_usd: float
    one_contract_credit_usd: Optional[float]
    one_contract_max_loss_usd: float
    allocation_budget_usd: float
    live_cap_usd: float
    max_affordable_quantity: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "intent_id": self.intent_id,
            "underlying": self.underlying,
            "side": self.side,
            "direction": self.direction,
            "structure": self.structure,
            "quote_observed_at_utc": self.quote_observed_at_utc,
            "quote_age_seconds": self.quote_age_seconds,
            "expiry": self.expiry,
            "dte": self.dte,
            "legs": [leg.to_dict() for leg in self.legs],
            "combo_bid_per_share": self.combo_bid_per_share,
            "combo_ask_per_share": self.combo_ask_per_share,
            "width_usd": self.width_usd,
            "primary_leg_delta": self.primary_leg_delta,
            "primary_leg_iv": self.primary_leg_iv,
            "bid_ask_spread_pct": self.bid_ask_spread_pct,
            "liquidity_status": self.liquidity_status,
            "one_contract_cost_usd": self.one_contract_cost_usd,
            "one_contract_credit_usd": self.one_contract_credit_usd,
            "one_contract_max_loss_usd": self.one_contract_max_loss_usd,
            "allocation_budget_usd": self.allocation_budget_usd,
            "live_cap_usd": self.live_cap_usd,
            "max_affordable_quantity": self.max_affordable_quantity,
        }


_STAGE_A_KEYS = frozenset({
    "underlying", "side", "direction", "structure", "target_dte",
    "intended_hold_days", "target_delta", "conviction",
    "allocation_pct_net_liq", "alpha", "thesis",
})
_LEG_KEYS = frozenset({
    "con_id", "action", "right", "strike", "bid_per_share",
    "ask_per_share", "delta", "iv",
})
_CANDIDATE_KEYS = frozenset({
    "candidate_id", "intent_id", "underlying", "side", "direction",
    "structure", "quote_observed_at_utc", "quote_age_seconds", "expiry",
    "dte", "legs", "combo_bid_per_share", "combo_ask_per_share",
    "width_usd", "primary_leg_delta", "primary_leg_iv",
    "bid_ask_spread_pct", "liquidity_status", "one_contract_cost_usd",
    "one_contract_credit_usd", "one_contract_max_loss_usd",
    "allocation_budget_usd", "live_cap_usd", "max_affordable_quantity",
})


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EntryContractError("%s must be a JSON object" % label)
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset, label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise EntryContractError(
            "%s keys mismatch; missing=%s extra=%s" % (label, missing, extra)
        )


def _string(value: Any, key: str, min_length: int = 0, max_length: Optional[int] = None) -> str:
    if not isinstance(value, str):
        raise EntryContractError("%s must be a string" % key)
    if len(value) < min_length or (max_length is not None and len(value) > max_length):
        raise EntryContractError("%s length is outside the frozen bounds" % key)
    return value


def _integer(value: Any, key: str, minimum: int, maximum: Optional[int] = None) -> int:
    if type(value) is not int:
        raise EntryContractError("%s must be an integer, not %r" % (key, value))
    if value < minimum or (maximum is not None and value > maximum):
        raise EntryContractError("%s is outside the frozen bounds" % key)
    return value


def _number(
    value: Any,
    key: str,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    exclusive_minimum: bool = False,
) -> float:
    if type(value) not in (int, float):
        raise EntryContractError("%s must be a finite number, not %r" % (key, value))
    number = float(value)
    if not math.isfinite(number):
        raise EntryContractError("%s must be finite" % key)
    if minimum is not None:
        if exclusive_minimum and not number > minimum:
            raise EntryContractError("%s must be greater than %s" % (key, minimum))
        if not exclusive_minimum and number < minimum:
            raise EntryContractError("%s must be at least %s" % (key, minimum))
    if maximum is not None and number > maximum:
        raise EntryContractError("%s must be at most %s" % (key, maximum))
    return number


def _enum(value: Any, key: str, allowed: frozenset) -> str:
    string = _string(value, key)
    if string not in allowed:
        raise EntryContractError("%s has non-canonical value %r" % (key, string))
    return string


def _parse_stage_a_intent(value: Any, label: str = "intent") -> StageAIntent:
    obj = _object(value, label)
    _exact_keys(obj, _STAGE_A_KEYS, label)
    underlying = _string(obj["underlying"], "%s.underlying" % label)
    if not _UNDERLYING_RE.fullmatch(underlying):
        raise EntryContractError("%s.underlying is not canonical" % label)
    side = _enum(obj["side"], "%s.side" % label, SIDES)
    direction = _enum(obj["direction"], "%s.direction" % label, DIRECTIONS)
    structure = _enum(obj["structure"], "%s.structure" % label, STRUCTURES)
    target_dte = _integer(obj["target_dte"], "%s.target_dte" % label, 1, 800)
    intended_hold_days = _integer(
        obj["intended_hold_days"], "%s.intended_hold_days" % label, 1, 365
    )
    if intended_hold_days > target_dte:
        raise EntryContractError("intended_hold_days may not exceed target_dte")
    target_delta = _number(
        obj["target_delta"], "%s.target_delta" % label,
        minimum=0, maximum=1, exclusive_minimum=True,
    )
    conviction = _integer(obj["conviction"], "%s.conviction" % label, 1, 10)
    allocation = _number(
        obj["allocation_pct_net_liq"], "%s.allocation_pct_net_liq" % label,
        minimum=0, maximum=100, exclusive_minimum=True,
    )
    alpha = _string(obj["alpha"], "%s.alpha" % label, 1, 600)
    thesis = _string(obj["thesis"], "%s.thesis" % label, 1, 1200)

    if side == "credit":
        if structure != "cash secured put" or direction != "bullish":
            raise EntryContractError("credit intent must be a bullish cash secured put")
    elif structure == "cash secured put":
        raise EntryContractError("cash secured put requires side=credit")
    if structure in ("long call", "call debit spread") and direction != "bullish":
        raise EntryContractError("call debit structures require direction=bullish")
    if structure in ("long put", "put debit spread") and direction != "bearish":
        raise EntryContractError("put debit structures require direction=bearish")

    return StageAIntent(
        underlying=underlying,
        side=side,
        direction=direction,
        structure=structure,
        target_dte=target_dte,
        intended_hold_days=intended_hold_days,
        target_delta=target_delta,
        conviction=conviction,
        allocation_pct_net_liq=allocation,
        alpha=alpha,
        thesis=thesis,
    )


def parse_stage_a(raw: Union[str, bytes, bytearray]) -> List[StageAIntent]:
    obj = _object(parse_json_document(raw), "Stage A output")
    _exact_keys(obj, frozenset({"intents"}), "Stage A output")
    values = obj["intents"]
    if not isinstance(values, list):
        raise EntryContractError("Stage A intents must be an array")
    if len(values) > 3:
        raise EntryContractError("Stage A may contain at most 3 intents")
    return [_parse_stage_a_intent(value, "intents[%d]" % index)
            for index, value in enumerate(values)]





DEBIT_HOLD_FLOOR_MULTIPLE = 8


def hold_dte_floor(intent: StageAIntent) -> int:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(intent, StageAIntent):
        raise EntryContractError("intent must be StageAIntent")
    if intent.side == "credit":
        return max(3, intent.intended_hold_days)






    return intent.intended_hold_days * DEBIT_HOLD_FLOOR_MULTIPLE


def _parse_runtime_leg(value: Any, label: str) -> RuntimeLeg:
    obj = _object(value, label)
    _exact_keys(obj, _LEG_KEYS, label)
    leg = RuntimeLeg(
        con_id=_integer(obj["con_id"], "%s.con_id" % label, 1),
        action=_enum(obj["action"], "%s.action" % label, LEG_ACTIONS),
        right=_enum(obj["right"], "%s.right" % label, LEG_RIGHTS),
        strike=_number(obj["strike"], "%s.strike" % label,
                       minimum=0, exclusive_minimum=True),
        bid_per_share=_number(obj["bid_per_share"], "%s.bid_per_share" % label,
                              minimum=0, exclusive_minimum=True),
        ask_per_share=_number(obj["ask_per_share"], "%s.ask_per_share" % label,
                              minimum=0, exclusive_minimum=True),
        delta=_number(obj["delta"], "%s.delta" % label, minimum=-1, maximum=1),
        iv=_number(obj["iv"], "%s.iv" % label, minimum=0, exclusive_minimum=True),
    )
    if leg.ask_per_share < leg.bid_per_share:
        raise EntryContractError("%s has a crossed bid/ask" % label)
    return leg


def parse_runtime_candidate(value: Any, label: str = "candidate") -> RuntimeCandidate:
    obj = _object(value, label)
    _exact_keys(obj, _CANDIDATE_KEYS, label)
    candidate_id = _string(obj["candidate_id"], "%s.candidate_id" % label)
    if not _CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise EntryContractError("%s.candidate_id is not canonical" % label)
    intent_id = _string(obj["intent_id"], "%s.intent_id" % label)
    if not _INTENT_ID_RE.fullmatch(intent_id):
        raise EntryContractError("%s.intent_id is not canonical" % label)
    underlying = _string(obj["underlying"], "%s.underlying" % label)
    if not _UNDERLYING_RE.fullmatch(underlying):
        raise EntryContractError("%s.underlying is not canonical" % label)
    expiry = _string(obj["expiry"], "%s.expiry" % label)
    if not _EXPIRY_RE.fullmatch(expiry):
        raise EntryContractError("%s.expiry must be YYYYMMDD" % label)
    try:
        datetime.strptime(expiry, "%Y%m%d")
    except ValueError as exc:
        raise EntryContractError("%s.expiry is not a calendar date" % label) from exc
    observed = _string(obj["quote_observed_at_utc"], "%s.quote_observed_at_utc" % label)
    try:
        parsed_observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EntryContractError("%s.quote_observed_at_utc is not ISO-8601" % label) from exc
    if parsed_observed.tzinfo is None or parsed_observed.utcoffset() != timezone.utc.utcoffset(None):
        raise EntryContractError("%s.quote_observed_at_utc must be UTC" % label)
    raw_legs = obj["legs"]
    if not isinstance(raw_legs, list) or not (1 <= len(raw_legs) <= 2):
        raise EntryContractError("%s.legs must contain 1 or 2 legs" % label)
    legs = [_parse_runtime_leg(v, "%s.legs[%d]" % (label, i))
            for i, v in enumerate(raw_legs)]
    if len({leg.con_id for leg in legs}) != len(legs):
        raise EntryContractError("%s contains duplicate con_id legs" % label)

    width_raw = obj["width_usd"]
    width = None if width_raw is None else _number(
        width_raw, "%s.width_usd" % label, minimum=0, exclusive_minimum=True
    )
    credit_raw = obj["one_contract_credit_usd"]
    credit = None if credit_raw is None else _number(
        credit_raw, "%s.one_contract_credit_usd" % label,
        minimum=0, exclusive_minimum=True,
    )
    liquidity = _string(obj["liquidity_status"], "%s.liquidity_status" % label)
    if liquidity != "pass":
        raise EntryContractError("%s.liquidity_status must be exactly 'pass'" % label)

    return RuntimeCandidate(
        candidate_id=candidate_id,
        intent_id=intent_id,
        underlying=underlying,
        side=_enum(obj["side"], "%s.side" % label, SIDES),
        direction=_enum(obj["direction"], "%s.direction" % label, DIRECTIONS),
        structure=_enum(obj["structure"], "%s.structure" % label, STRUCTURES),
        quote_observed_at_utc=observed,
        quote_age_seconds=_number(
            obj["quote_age_seconds"], "%s.quote_age_seconds" % label, minimum=0
        ),
        expiry=expiry,
        dte=_integer(obj["dte"], "%s.dte" % label, 1),
        legs=legs,
        combo_bid_per_share=_number(
            obj["combo_bid_per_share"], "%s.combo_bid_per_share" % label,
            minimum=0, exclusive_minimum=True,
        ),
        combo_ask_per_share=_number(
            obj["combo_ask_per_share"], "%s.combo_ask_per_share" % label,
            minimum=0, exclusive_minimum=True,
        ),
        width_usd=width,
        primary_leg_delta=_number(
            obj["primary_leg_delta"], "%s.primary_leg_delta" % label,
            minimum=0, maximum=1,
        ),
        primary_leg_iv=_number(
            obj["primary_leg_iv"], "%s.primary_leg_iv" % label,
            minimum=0, exclusive_minimum=True,
        ),
        bid_ask_spread_pct=_number(
            obj["bid_ask_spread_pct"], "%s.bid_ask_spread_pct" % label,
            minimum=0,
        ),
        liquidity_status=liquidity,
        one_contract_cost_usd=_number(
            obj["one_contract_cost_usd"], "%s.one_contract_cost_usd" % label,
            minimum=0, exclusive_minimum=True,
        ),
        one_contract_credit_usd=credit,
        one_contract_max_loss_usd=_number(
            obj["one_contract_max_loss_usd"], "%s.one_contract_max_loss_usd" % label,
            minimum=0, exclusive_minimum=True,
        ),
        allocation_budget_usd=_number(
            obj["allocation_budget_usd"], "%s.allocation_budget_usd" % label,
            minimum=0,
        ),
        live_cap_usd=_number(obj["live_cap_usd"], "%s.live_cap_usd" % label, minimum=0),
        max_affordable_quantity=_integer(
            obj["max_affordable_quantity"], "%s.max_affordable_quantity" % label, 1
        ),
    )


def canonical_candidate_id(
    candidate: Optional[RuntimeCandidate] = None,
    *,
    intent_id: Optional[str] = None,
    underlying: Optional[str] = None,
    side: Optional[str] = None,
    direction: Optional[str] = None,
    structure: Optional[str] = None,
    expiry: Optional[str] = None,
    legs: Optional[Sequence[RuntimeLeg]] = None,
) -> str:
    """Public API contract; production-derived narrative omitted."""
    if candidate is not None:
        if any(value is not None for value in
               (intent_id, underlying, side, direction, structure, expiry, legs)):
            raise EntryContractError("pass a candidate or identity fields, not both")
        intent_id = candidate.intent_id
        underlying = candidate.underlying
        side = candidate.side
        direction = candidate.direction
        structure = candidate.structure
        expiry = candidate.expiry
        legs = candidate.legs
    values = (intent_id, underlying, side, direction, structure, expiry, legs)
    if any(value is None for value in values):
        raise EntryContractError("candidate identity fields are incomplete")
    assert legs is not None
    if not legs or any(not isinstance(leg, RuntimeLeg) for leg in legs):
        raise EntryContractError("candidate identity requires RuntimeLeg objects")
    payload = {
        "contract_version": CONTRACT_VERSION,
        "intent_id": intent_id,
        "underlying": underlying,
        "side": side,
        "direction": direction,
        "structure": structure,
        "expiry": expiry,
        "legs": [leg.identity_dict() for leg in legs],
    }
    try:
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EntryContractError("candidate identity is not canonical JSON") from exc
    return "cand_" + hashlib.sha256(canonical).hexdigest()


def _decimal(value: float) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise EntryContractError("invalid decimal value %r" % value) from exc
    if not result.is_finite():
        raise EntryContractError("non-finite decimal value")
    return result


def _money_matches(actual: float, expected: float) -> bool:
    return abs(_decimal(actual) - _decimal(expected)) <= _MONEY_TOLERANCE


def _float_matches(actual: float, expected: float, tolerance: float = _FLOAT_TOLERANCE) -> bool:
    return math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance)


def validate_runtime_candidate(
    candidate: RuntimeCandidate,
    intent_id: str,
    intent: StageAIntent,
) -> RuntimeCandidate:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(candidate, RuntimeCandidate):
        raise EntryContractError("candidate must be RuntimeCandidate")
    if not isinstance(intent, StageAIntent):
        raise EntryContractError("intent must be StageAIntent")
    if not _INTENT_ID_RE.fullmatch(intent_id):
        raise EntryContractError("intent_id is not canonical")
    expected_binding = (intent_id, intent.underlying, intent.side, intent.direction, intent.structure)
    actual_binding = (
        candidate.intent_id, candidate.underlying, candidate.side,
        candidate.direction, candidate.structure,
    )
    if actual_binding != expected_binding:
        raise EntryContractError("candidate does not exactly match its Stage A intent")
    if candidate.candidate_id != canonical_candidate_id(candidate):
        raise EntryContractError("candidate_id does not bind the canonical conId identity")


    checked = parse_runtime_candidate(candidate.to_dict())
    legs = checked.legs
    structure = checked.structure
    if structure == "long call":
        expected = ("debit", "bullish", 1, "buy", "call")
    elif structure == "long put":
        expected = ("debit", "bearish", 1, "buy", "put")
    elif structure == "call debit spread":
        expected = ("debit", "bullish", 2, "buy", "call")
    elif structure == "put debit spread":
        expected = ("debit", "bearish", 2, "buy", "put")
    else:
        expected = ("credit", "bullish", 1, "sell", "put")
    expected_side, expected_direction, leg_count, first_action, right = expected
    if checked.side != expected_side or checked.direction != expected_direction:
        raise EntryContractError("candidate side/direction conflicts with structure")
    if len(legs) != leg_count or legs[0].action != first_action or legs[0].right != right:
        raise EntryContractError("candidate leg shape conflicts with structure")

    if not _float_matches(checked.primary_leg_delta, abs(legs[0].delta)):
        raise EntryContractError("primary_leg_delta does not match the first leg")
    if not _float_matches(checked.primary_leg_iv, legs[0].iv):
        raise EntryContractError("primary_leg_iv does not match the first leg")

    if structure in ("call debit spread", "put debit spread"):
        short = legs[1]
        if short.action != "sell" or short.right != right:
            raise EntryContractError("debit spread second leg must be the matching sell leg")
        if structure == "call debit spread" and not legs[0].strike < short.strike:
            raise EntryContractError("call debit spread buy strike must be below sell strike")
        if structure == "put debit spread" and not legs[0].strike > short.strike:
            raise EntryContractError("put debit spread buy strike must be above sell strike")
        expected_width = abs(short.strike - legs[0].strike)
        if checked.width_usd is None or not _money_matches(checked.width_usd, expected_width):
            raise EntryContractError("spread width does not match its strikes")
        if checked.combo_ask_per_share > checked.width_usd + 0.01:
            raise EntryContractError("debit spread ask exceeds its width")
    else:
        if checked.width_usd is not None:
            raise EntryContractError("non-spread candidate width_usd must be null")




    if checked.combo_ask_per_share < checked.combo_bid_per_share:
        raise EntryContractError("candidate combo quote is crossed")
    mid = (checked.combo_bid_per_share + checked.combo_ask_per_share) / 2.0
    expected_spread_pct = (checked.combo_ask_per_share - checked.combo_bid_per_share) / mid * 100.0
    if not math.isclose(
        checked.bid_ask_spread_pct, expected_spread_pct, rel_tol=0.0, abs_tol=0.02
    ):
        raise EntryContractError("bid_ask_spread_pct does not match the combo quote")

    if checked.side == "debit":
        if checked.one_contract_credit_usd is not None:
            raise EntryContractError("debit candidate credit must be null")
        expected_cost = checked.combo_ask_per_share * 100.0
        expected_max_loss = expected_cost
        if checked.dte < hold_dte_floor(intent):
            raise EntryContractError("debit candidate violates the intended-hold DTE floor")
    else:
        if checked.one_contract_credit_usd is None:
            raise EntryContractError("cash secured put credit must be present")
        if checked.width_usd is not None:
            raise EntryContractError("cash secured put width must be null")
        if not (3 <= checked.dte <= 45) or checked.dte < hold_dte_floor(intent):
            raise EntryContractError("cash secured put violates the 3-45 DTE/hold window")
        expected_cost = legs[0].strike * 100.0
        expected_credit = checked.combo_bid_per_share * 100.0
        if not _money_matches(checked.one_contract_credit_usd, expected_credit):
            raise EntryContractError("cash secured put credit does not match executable bid")
        expected_max_loss = expected_cost - expected_credit
        if expected_max_loss <= 0:
            raise EntryContractError("cash secured put has non-positive maximum loss")

    if not _money_matches(checked.one_contract_cost_usd, expected_cost):
        raise EntryContractError("one_contract_cost_usd has the wrong units/arithmetic")
    if not _money_matches(checked.one_contract_max_loss_usd, expected_max_loss):
        raise EntryContractError("one_contract_max_loss_usd arithmetic mismatch")













    spendable = checked.live_cap_usd
    expected_quantity = int(math.floor((spendable + 1e-9) / checked.one_contract_cost_usd))
    if expected_quantity < 1:
        raise EntryContractError("candidate is unaffordable")
    if checked.max_affordable_quantity != expected_quantity:
        raise EntryContractError("max_affordable_quantity arithmetic mismatch")
    return checked


def validate_candidates(
    intent_id: str,
    intent: StageAIntent,
    candidates: Sequence[RuntimeCandidate],
) -> List[RuntimeCandidate]:
    if not isinstance(candidates, (list, tuple)):
        raise EntryContractError("candidates must be a list")
    if not (3 <= len(candidates) <= 5):
        raise EntryContractError("Stage B requires 3 to 5 prefiltered candidates")
    checked = [validate_runtime_candidate(candidate, intent_id, intent)
               for candidate in candidates]
    ids = [candidate.candidate_id for candidate in checked]
    if len(set(ids)) != len(ids):
        raise EntryContractError("duplicate candidate IDs/economic identities")
    return checked


def parse_stage_b_input(
    raw: Union[str, bytes, bytearray],
) -> Tuple[str, StageAIntent, List[RuntimeCandidate]]:
    obj = _object(parse_json_document(raw), "Stage B input")
    _exact_keys(obj, frozenset({"intent_id", "intent", "candidates"}), "Stage B input")
    intent_id = _string(obj["intent_id"], "Stage B input.intent_id")
    if not _INTENT_ID_RE.fullmatch(intent_id):
        raise EntryContractError("Stage B input intent_id is not canonical")
    intent = _parse_stage_a_intent(obj["intent"], "Stage B input.intent")
    raw_candidates = obj["candidates"]
    if not isinstance(raw_candidates, list):
        raise EntryContractError("Stage B candidates must be an array")
    candidates = [parse_runtime_candidate(value, "candidates[%d]" % index)
                  for index, value in enumerate(raw_candidates)]
    return intent_id, intent, validate_candidates(intent_id, intent, candidates)


def parse_stage_b(
    raw: Union[str, bytes, bytearray],
    candidates: Sequence[RuntimeCandidate],
) -> Optional[str]:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(candidates, (list, tuple)):
        raise EntryContractError("candidates must be a list")
    if not (3 <= len(candidates) <= 5):
        raise EntryContractError("Stage B requires 3 to 5 prefiltered candidates")
    candidate_ids: List[str] = []
    for candidate in candidates:
        if not isinstance(candidate, RuntimeCandidate):
            raise EntryContractError("candidates must contain RuntimeCandidate objects")
        if not _CANDIDATE_ID_RE.fullmatch(candidate.candidate_id):
            raise EntryContractError("candidate list contains a malformed candidate_id")
        if candidate.candidate_id != canonical_candidate_id(candidate):
            raise EntryContractError("candidate list contains an unbound candidate_id")
        candidate_ids.append(candidate.candidate_id)
    if len(candidate_ids) != len(set(candidate_ids)):
        raise EntryContractError("candidate list contains duplicate IDs")

    obj = _object(parse_json_document(raw), "Stage B output")
    keys = frozenset(obj)
    if keys == frozenset({"decline"}):
        if obj["decline"] is not True:
            raise EntryContractError("terminal decline must be exactly {\"decline\":true}")
        return None
    if keys == frozenset({"candidate_id"}):
        selected = _string(obj["candidate_id"], "Stage B output.candidate_id")
        if not _CANDIDATE_ID_RE.fullmatch(selected):
            raise EntryContractError("selected candidate_id is not canonical")
        if selected not in set(candidate_ids):
            raise EntryContractError("selected candidate_id was not supplied to Stage B")
        return selected
    raise EntryContractError(
        "Stage B output must contain exactly candidate_id or terminal decline"
    )


__all__ = [
    "CONTRACT_VERSION",
    "CONTRACT_SHA",
    "CONTRACT_SHA256",
    "SCHEMA_PATH",
    "EntryContractError",
    "StageAIntent",
    "RuntimeLeg",
    "RuntimeCandidate",
    "load_frozen_schema",
    "parse_json_document",
    "parse_stage_a",
    "hold_dte_floor",
    "parse_runtime_candidate",
    "canonical_candidate_id",
    "validate_runtime_candidate",
    "validate_candidates",
    "parse_stage_b_input",
    "parse_stage_b",
]
