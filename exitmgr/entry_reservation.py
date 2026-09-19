"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Optional


LEDGER_VERSION = 1
MIGRATION_SCHEMA = "entry_reservation_migration.v2"
LEGACY_FENCE_SCHEMA = "entry_reservation_legacy_fence.v1"
DEFAULT_TTL_SECONDS = 15 * 60.0



DEFAULT_MAX_OBSERVATION_AGE_S = 90.0




SLOT_PREDICATE_ENABLED = True


def _default_ledger_stem() -> str:
    """Public API contract; production-derived narrative omitted."""
    if "pytest" in sys.modules:
        return tempfile.gettempdir() + "/alfred-entry-reservations-pytest-%d" % os.getpid()
    return str(Path.home() / ".local/var/exitmgr/entry-reservations")


def default_ledger_path() -> Path:
    """Public API contract; production-derived narrative omitted."""
    return Path(os.environ.get("EXITMGR_ENTRY_RESERVATIONS")
                or (_default_ledger_stem() + ".json"))


def default_lock_path() -> Path:
    return Path(os.environ.get("EXITMGR_ENTRY_RESERVATION_LOCK")
                or (_default_ledger_stem() + ".lock"))


def legacy_ledger_path() -> Path:
    """Public API contract; production-derived narrative omitted."""
    return Path("/tmp/alfred-entry-reservations.json")


def legacy_lock_path() -> Path:
    return Path("/tmp/alfred-entry-reservations.lock")


def default_migration_receipt_path(ledger_path: Path) -> Path:
    return ledger_path.with_name(ledger_path.stem + ".migration.json")


def default_legacy_backup_path(ledger_path: Path) -> Path:
    return ledger_path.with_name(ledger_path.stem + ".legacy-source.json")


DEFAULT_LEDGER_PATH = default_ledger_path()
DEFAULT_LOCK_PATH = default_lock_path()
RELEASING_STATUSES = frozenset({"cancelled", "apicancelled", "inactive", "rejected"})


INTENT_RESOLUTIONS = frozenset({
    "journaled",
    "terminal_no_fill",
    "not_transmitted",
})
INTENT_TRANSMIT_STATES = frozenset({"unknown", "yes", "no"})
_EPS = 1e-9






ADMISSION_DIMENSIONS = (
    "order_ref", "envelope_id", "code_version", "policy_version",
    "con_id", "leg_con_ids", "symbol", "sector_cluster",
    "structure", "side", "capital_usd", "collateral_usd", "contracts",
    "net_liq", "available_funds", "broker_deployed_usd",
    "observation_readable", "observed_at_monotonic", "max_observation_age_s",
    "open_position_count", "max_concurrent",
    "name_aggregate_usd", "name_cap_usd",
    "sector_aggregate_usd", "sector_cap_usd",
    "deployed_aggregate_usd", "deployed_cap_usd",
    "day_orders", "max_orders_per_day", "day_notional", "max_notional_per_day",
    "open_campaign_con_ids", "open_campaigns", "campaign_conflict_symbols",
    "campaign_add_intent",
    "final_contract", "structured_intent", "markers_clear", "visible_order_refs",
)


OPTIONAL_ADMISSION_DIMENSIONS = frozenset({"reflected_debit_positions"})


def _json_safe(value, *, name: str):
    """Public API contract; production-derived narrative omitted."""
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value, allow_nan=False, default=str))
    except Exception as exc:
        raise EntryReservationError(f"{name} is not JSON-serialisable: {exc}") from exc


def _normalize_intent(payload, *, now: float) -> dict:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(payload, Mapping):
        raise EntryReservationError("entry intent must be a mapping")
    ref = _order_ref(payload.get("order_ref"))
    qty = int(_finite(payload.get("requested_qty"), name=f"{ref}.requested_qty", positive=True))
    transmitted = str(payload.get("transmitted", "unknown") or "unknown").lower()
    if transmitted not in INTENT_TRANSMIT_STATES:
        raise EntryReservationError(f"intent {ref!r} has an unknown transmit state {transmitted!r}")
    created = _finite(payload.get("created_at", now), name=f"{ref}.created_at")
    return {
        "order_ref": ref,
        "con_id": _con_id(payload.get("con_id")),
        "leg_con_ids": list(_con_ids(payload.get("leg_con_ids") or (payload.get("con_id"),),
                                     name=f"{ref}.leg_con_ids")),
        "symbol": _text(payload.get("symbol"), limit=32, upper=True),
        "side": (_text(payload.get("side"), limit=8).lower() or "debit"),
        "structure": _text(payload.get("structure"), limit=64),
        "source": _text(payload.get("source"), limit=32),
        "decision_id": _text(payload.get("decision_id"), limit=128),


        "code_version": _optional_version(payload.get("code_version"), name="code_version",
                                            length=40),
        "policy_version": _optional_version(payload.get("policy_version"), name="policy_version",
                                              length=64),
        "requested_qty": qty,
        "estimated_debit": (None if payload.get("estimated_debit") is None
                            else _finite(payload.get("estimated_debit"),
                                         name=f"{ref}.estimated_debit")),
        "created_at": created,
        "updated_at": _finite(payload.get("updated_at", now), name=f"{ref}.updated_at"),
        "transmitted": transmitted,
        "journaled": bool(payload.get("journaled", False)),
        "observations": max(0, int(_finite(payload.get("observations", 0) or 0,
                                           name=f"{ref}.observations", non_negative=True))),
        "journal_template": _json_safe(payload.get("journal_template"),
                                       name=f"{ref}.journal_template"),
        "final_contract": _json_safe(payload.get("final_contract"),
                                      name=f"{ref}.final_contract"),
        "structured_intent": _json_safe(payload.get("structured_intent"),
                                         name=f"{ref}.structured_intent"),
        "campaign_add_intent": _json_safe(payload.get("campaign_add_intent"),
                                           name=f"{ref}.campaign_add_intent"),
        "admission_receipt": _json_safe(payload.get("admission_receipt"),
                                         name=f"{ref}.admission_receipt"),
    }


class EntryReservationError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


class _PlacementFailure(Exception):
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, original: BaseException, invoked: bool):
        super().__init__(str(original))
        self.original = original
        self.invoked = invoked


@dataclass(frozen=True)
class ReservationDecision:
    """Public API contract; production-derived narrative omitted."""

    allowed: bool
    should_place: bool
    status: str
    reasons: tuple[str, ...] = ()
    order_ref: str = ""
    con_id: int = 0
    collateral_usd: float = 0.0
    outstanding_unreflected_usd: float = 0.0
    effective_deployed_usd: float = 0.0
    effective_available_funds: float = 0.0


    side: str = ""
    symbol: str = ""
    capital_usd: float = 0.0
    place_invoked: bool = False
    effective_open_positions: int = 0
    reserved_slots: int = 0
    reflected_debit_order_refs: tuple[str, ...] = ()
    reflected_debit_capital_usd: float = 0.0
    reflected_debit_actual_usd: float = 0.0
    admission_receipt: Optional[dict] = None


@dataclass(frozen=True)
class ReconcileResult:
    removed_visible: tuple[str, ...]
    removed_expired: tuple[str, ...]
    outstanding_unreflected_usd: float
    active_count: int


def _finite(value, *, name: str, positive: bool = False,
            non_negative: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EntryReservationError(f"{name} is not numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise EntryReservationError(f"{name} is not finite: {value!r}")
    if positive and number <= 0:
        raise EntryReservationError(f"{name} must be positive: {value!r}")
    if non_negative and number < 0:
        raise EntryReservationError(f"{name} must be non-negative: {value!r}")
    return number


def _optional_cap(value, *, name: str) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    if value is None:
        return None
    return _finite(value, name=name, non_negative=True)


def _optional_int_cap(value, *, name: str) -> Optional[int]:
    if value is None:
        return None
    number = _finite(value, name=name, non_negative=True)
    return int(number)


def _bool(value, *, name: str) -> bool:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(value, bool):
        raise EntryReservationError(f"{name} must be a bool, got {type(value).__name__}")
    return value


def _order_ref(value) -> str:
    ref = str(value or "").strip()
    if not ref or len(ref) > 512:
        raise EntryReservationError("order_ref must be a non-empty string of at most 512 chars")
    return ref


def _optional_version(value, *, name: str, length: int) -> str:
    """Public API contract; production-derived narrative omitted."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) != length or any(ch not in "0123456789abcdef" for ch in text):
        raise EntryReservationError("%s must be lowercase %d-hex" % (name, length))
    return text


def _required_version(value, *, name: str, length: int) -> str:
    text = _optional_version(value, name=name, length=length)
    if not text:
        raise EntryReservationError("%s is required" % name)
    return text


def _con_id(value) -> int:
    try:
        con_id = int(value)
    except (TypeError, ValueError) as exc:
        raise EntryReservationError(f"con_id is invalid: {value!r}") from exc
    if con_id <= 0:
        raise EntryReservationError(f"con_id must be positive: {value!r}")
    return con_id


def _con_ids(values, *, name: str) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise EntryReservationError(f"{name} must be a collection of contract ids")
    return tuple(sorted({_con_id(v) for v in (values or ())}))


def _visible_refs(values: Optional[Iterable]) -> frozenset[str]:
    return frozenset(str(value).strip() for value in (values or ()) if str(value).strip())


def _text(value, *, limit: int, upper: bool = False) -> str:
    out = str(value or "").strip()
    if upper:
        out = out.upper()
    return out[:limit]


def _open_campaign_records(values) -> tuple[dict, ...]:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(values, (tuple, list)):
        raise EntryReservationError("open_campaigns must be a collection")
    out, ids = [], set()
    for index, raw in enumerate(values):
        if not isinstance(raw, Mapping):
            raise EntryReservationError(f"open_campaigns[{index}] must be a mapping")
        campaign_id = _text(raw.get("campaign_id"), limit=256)
        if not campaign_id or campaign_id in ids:
            raise EntryReservationError("open campaign_id must be non-empty and unique")
        primary = _con_id(raw.get("primary_con_id"))
        legs = _con_ids(raw.get("leg_con_ids"), name=f"open_campaigns[{index}].leg_con_ids")
        if not legs or primary not in legs:
            raise EntryReservationError("open campaign primary must be present in its legs")
        contracts_value = _finite(raw.get("contracts"), name="open campaign contracts",
                                  positive=True)
        if contracts_value != int(contracts_value):
            raise EntryReservationError("open campaign contracts must be a whole number")
        risk = round(_finite(raw.get("risk_usd"), name="open campaign risk",
                             non_negative=True), 2)
        risk_known = _bool(raw.get("risk_known"), name="open campaign risk_known")
        if risk_known and risk <= 0:
            raise EntryReservationError("known open campaign risk must be positive")
        symbol = _text(raw.get("symbol"), limit=32, upper=True)
        if not symbol:
            raise EntryReservationError("open campaign symbol is required")
        out.append({
            "campaign_id": campaign_id, "primary_con_id": primary,
            "leg_con_ids": list(legs), "symbol": symbol,
            "contracts": int(contracts_value), "risk_usd": risk,
            "risk_known": risk_known,
        })
        ids.add(campaign_id)
    return tuple(out)


def _campaign_add_document(value) -> Optional[dict]:
    """Public API contract; production-derived narrative omitted."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise EntryReservationError("campaign_add_intent must be a mapping or null")
    schema = _text(value.get("schema"), limit=64)
    action = _text(value.get("action"), limit=32).lower()
    if schema != "campaign_add.v1" or action not in {"add", "scale_in"}:
        raise EntryReservationError("campaign add requires schema campaign_add.v1 and add/scale_in action")
    intent_id = _text(value.get("intent_id"), limit=256)
    campaign_id = _text(value.get("campaign_id"), limit=256)
    reason = _text(value.get("reason"), limit=1024)
    if not intent_id or not campaign_id or not reason:
        raise EntryReservationError("campaign add requires intent_id, campaign_id and reason")
    primary = _con_id(value.get("primary_con_id"))
    legs = _con_ids(value.get("leg_con_ids"), name="campaign_add_intent.leg_con_ids")
    if not legs or primary not in legs:
        raise EntryReservationError("campaign add primary must be present in its legs")
    max_contracts_value = _finite(
        value.get("max_campaign_contracts"), name="max_campaign_contracts", positive=True)
    if max_contracts_value != int(max_contracts_value):
        raise EntryReservationError("max_campaign_contracts must be a whole number")
    return {
        "schema": schema, "action": action, "intent_id": intent_id,
        "campaign_id": campaign_id, "primary_con_id": primary,
        "leg_con_ids": list(legs),
        "max_campaign_contracts": int(max_contracts_value),
        "max_campaign_risk_usd": round(_finite(
            value.get("max_campaign_risk_usd"), name="max_campaign_risk_usd",
            positive=True), 2),
        "reason": reason,
    }


def _debit_reflection_proofs(values) -> tuple[dict, ...]:
    """Public API contract; production-derived narrative omitted."""
    if not isinstance(values, (tuple, list)):
        return ()
    proofs = []
    refs, occupied_legs = set(), set()

    def positive_int(value):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("integer evidence required")
        number = _finite(value, name="reflection integer", positive=True)
        if number != int(number):
            raise ValueError("fractional integer evidence")
        return int(number)

    try:
        for raw in values:
            if not isinstance(raw, Mapping):
                return ()
            ref = raw["order_ref"]
            if not isinstance(ref, str) or _order_ref(ref) != ref:
                return ()
            cid = positive_int(raw["con_id"])
            raw_legs = raw["leg_con_ids"]
            if not isinstance(raw_legs, (tuple, list)) or len(raw_legs) not in (1, 2):
                return ()
            legs = tuple(sorted(positive_int(v) for v in raw_legs))
            if len(set(legs)) != len(legs) or cid not in legs:
                return ()
            if ref in refs or occupied_legs.intersection(legs):
                return ()
            qty = positive_int(raw["contracts"])
            amount_raw = raw["capital_usd"]
            if isinstance(amount_raw, bool) or not isinstance(amount_raw, (int, float, str)):
                return ()
            capital = _finite(amount_raw, name="reflected debit", positive=True)
            symbol, cluster = raw["symbol"], raw["sector_cluster"]
            if (not isinstance(symbol, str) or not symbol
                    or _text(symbol, limit=32, upper=True) != symbol
                    or not isinstance(cluster, str) or not cluster
                    or _text(cluster, limit=64, upper=True) != cluster):
                return ()
            proof = dict(order_ref=ref, con_id=cid, leg_con_ids=legs, contracts=qty,
                         capital_usd=capital, symbol=symbol, sector_cluster=cluster)


            for key in ("name_counted_usd", "sector_counted_usd"):
                counted = raw.get(key, 0.0)
                if isinstance(counted, bool) or not isinstance(counted, (int, float, str)):
                    return ()
                counted = _finite(counted, name=key, non_negative=True)
                if counted and abs(counted - capital) > _EPS:
                    return ()
                proof[key] = counted
            refs.add(ref)
            occupied_legs.update(legs)
            proofs.append(proof)
    except (EntryReservationError, KeyError, TypeError, ValueError, OverflowError):
        return ()
    return tuple(proofs)


@contextmanager
def _exclusive_lock(path: Path, timeout_seconds: float, *, label: str):
    """Public API contract; production-derived narrative omitted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise EntryReservationError(f"{label} lock busy: {path}") from exc
                time.sleep(0.01)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class EntryReservationLedger:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, ledger_path=None, *, lock_path=None,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 max_collateral_pct: float = 0.80,
                 clock: Callable[[], float] = time.time,
                 lock_timeout_seconds: float = 2.0,
                 bootstrap_default: bool = False,
                 legacy_entry_processes_stopped: bool = False,
                 absent_legacy_verified_empty: bool = False):
        production_default = (ledger_path is None and lock_path is None
                              and "pytest" not in sys.modules)
        default_ledger = default_ledger_path()
        self.ledger_path = default_ledger if ledger_path is None else Path(ledger_path)
        if lock_path is not None:
            self.lock_path = Path(lock_path)
        elif self.ledger_path in (default_ledger, DEFAULT_LEDGER_PATH):
            self.lock_path = default_lock_path()
        else:
            self.lock_path = Path(str(self.ledger_path) + ".lock")
        if self.ledger_path == self.lock_path:
            raise ValueError("reservation ledger and lock paths must be different")
        self.ttl_seconds = _finite(ttl_seconds, name="ttl_seconds", positive=True)
        self.max_collateral_pct = _finite(
            max_collateral_pct, name="max_collateral_pct", positive=True)
        if self.max_collateral_pct > 1:
            raise ValueError("max_collateral_pct may not exceed 1.0")
        self.clock = clock
        self.lock_timeout_seconds = _finite(
            lock_timeout_seconds, name="lock_timeout_seconds", non_negative=True)
        self.migration_receipt_path = default_migration_receipt_path(self.ledger_path)
        self.legacy_backup_path = default_legacy_backup_path(self.ledger_path)
        self._production_default = production_default
        self._legacy_ledger_path = legacy_ledger_path()
        self._legacy_lock_path = legacy_lock_path()
        if bootstrap_default and not production_default:
            raise EntryReservationError(
                "default-storage bootstrap may not be combined with an explicit path or override")
        if production_default and bootstrap_default:
            self._bootstrap_default_storage(
                legacy_entry_processes_stopped=legacy_entry_processes_stopped,
                absent_legacy_verified_empty=absent_legacy_verified_empty)
        elif production_default:



            self._require_default_storage_ready()

    @contextmanager
    def _locked(self):
        with _exclusive_lock(self.lock_path, self.lock_timeout_seconds,
                             label="entry reservation"):
            if not self._production_default:
                yield
                return


            with _exclusive_lock(self._legacy_lock_path, self.lock_timeout_seconds,
                                 label="legacy entry reservation"):
                self._validate_completed_migration_locked(
                    legacy=self._legacy_ledger_path, receipt=self.migration_receipt_path)
                yield

    @staticmethod
    def _empty_state() -> dict:
        return {"version": LEDGER_VERSION, "reservations": {}, "intents": {}}

    def _load_state(self, path=None) -> dict:
        path = self.ledger_path if path is None else Path(path)
        if not path.exists():
            return self._empty_state()
        try:
            with path.open() as handle:
                state = json.load(handle)
        except Exception as exc:
            raise EntryReservationError(
                f"entry reservation ledger unreadable: {path}: {exc}") from exc
        if not isinstance(state, dict) or state.get("version") != LEDGER_VERSION:
            raise EntryReservationError("entry reservation ledger has an unsupported schema")
        reservations = state.get("reservations")
        if not isinstance(reservations, dict):
            raise EntryReservationError("entry reservation ledger reservations must be an object")
        checked = {}
        for key, raw in reservations.items():
            if not isinstance(raw, dict):
                raise EntryReservationError(f"reservation {key!r} is not an object")
            ref = _order_ref(raw.get("order_ref"))
            if ref != key:
                raise EntryReservationError(f"reservation key/order_ref mismatch for {key!r}")
            con_id = _con_id(raw.get("con_id"))
            collateral = _finite(
                raw.get("collateral_usd"), name=f"{ref}.collateral_usd", positive=True)
            created_at = _finite(raw.get("created_at"), name=f"{ref}.created_at")
            expires_at = _finite(raw.get("expires_at"), name=f"{ref}.expires_at")
            if expires_at <= created_at:
                raise EntryReservationError(f"reservation {ref!r} has an invalid expiry")








            side = _text(raw.get("side") or "credit", limit=8).lower()
            if side not in ("credit", "debit"):
                raise EntryReservationError(f"reservation {ref!r} has an unknown side {side!r}")
            capital = _finite(raw.get("capital_usd", collateral),
                              name=f"{ref}.capital_usd", positive=True)
            checked_row = {
                "order_ref": ref,
                "con_id": con_id,
                "collateral_usd": collateral,
                "created_at": created_at,
                "expires_at": expires_at,
                "envelope_id": _text(raw.get("envelope_id"), limit=128),
                "symbol": _text(raw.get("symbol"), limit=32, upper=True),
                "sector_cluster": _text(raw.get("sector_cluster"), limit=64, upper=True),
                "structure": _text(raw.get("structure"), limit=64),
                "side": side,
                "capital_usd": capital,
                "contracts": max(0, int(_finite(raw.get("contracts", 0) or 0,
                                                name=f"{ref}.contracts", non_negative=True))),
                "leg_con_ids": list(_con_ids(raw.get("leg_con_ids") or (con_id,),
                                             name=f"{ref}.leg_con_ids")),
                "observed_at": _finite(raw.get("observed_at", created_at),
                                       name=f"{ref}.observed_at"),



                "throttle_recorded": bool(raw.get("throttle_recorded", True)),
                "code_version": _text(raw.get("code_version"), limit=64),
                "policy_version": _text(raw.get("policy_version"), limit=64),
            }






            if "campaign_add_intent" in raw:
                checked_row["campaign_add_intent"] = _json_safe(
                    raw["campaign_add_intent"], name=f"{ref}.campaign_add_intent")
            if "admission_receipt" in raw:
                checked_row["admission_receipt"] = _json_safe(
                    raw["admission_receipt"], name=f"{ref}.admission_receipt")
            checked[ref] = checked_row




        raw_intents = state.get("intents")
        if raw_intents is None:
            raw_intents = {}
        if not isinstance(raw_intents, dict):
            raise EntryReservationError("entry reservation ledger intents must be an object")
        intents = {}
        for key, raw in raw_intents.items():
            row = _normalize_intent(raw, now=float(state.get("updated_at") or 0.0) or 0.0)
            if row["order_ref"] != key:
                raise EntryReservationError(f"intent key/order_ref mismatch for {key!r}")
            intents[key] = row
        result = {"version": LEDGER_VERSION, "reservations": checked, "intents": intents}
        origin = state.get("migration_origin_sha256")
        if origin is not None:
            result["migration_origin_sha256"] = _required_version(
                origin, name="migration_origin_sha256", length=64)
            generation = state.get("ledger_generation")
            if (isinstance(generation, bool) or not isinstance(generation, int)
                    or generation < 0):
                raise EntryReservationError("ledger_generation must be a non-negative integer")
            result["ledger_generation"] = generation
            result["previous_state_sha256"] = _required_version(
                state.get("previous_state_sha256"), name="previous_state_sha256", length=64)
        return result

    @staticmethod
    def _state_evidence_sha256(state: dict) -> str:
        payload = {
            "version": state.get("version"),
            "reservations": state.get("reservations"),
            "intents": state.get("intents"),
        }
        try:
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise EntryReservationError(
                f"entry reservation evidence is not serialisable: {exc}") from exc
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _active_state_sha256(state: dict) -> str:
        """Public API contract; production-derived narrative omitted."""
        payload = {
            "version": state.get("version"),
            "reservations": state.get("reservations"),
            "intents": state.get("intents"),
            "migration_origin_sha256": state.get("migration_origin_sha256"),
            "ledger_generation": state.get("ledger_generation"),
            "previous_state_sha256": state.get("previous_state_sha256"),
        }
        try:
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise EntryReservationError(
                f"active entry reservation state is not serialisable: {exc}") from exc
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except (AttributeError, OSError):

                pass
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _write_bytes_create_only(path: Path, payload: bytes) -> None:
        """Public API contract; production-derived narrative omitted."""
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise EntryReservationError(
                    f"legacy source backup unreadable: {path}: {exc}") from exc
            if existing != payload:
                raise EntryReservationError(
                    "legacy source backup already exists with different bytes")
            return
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except (AttributeError, OSError):
                pass
        except Exception:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise

    def _write_state_unbound(self, state: dict) -> None:
        payload = dict(state)
        payload["updated_at"] = _finite(self.clock(), name="clock")
        self._write_json_atomic(self.ledger_path, payload)

    def _write_state(self, state: dict) -> None:
        if not self._production_default:
            self._write_state_unbound(state)
            return
        receipt = self._read_migration_receipt(
            self.migration_receipt_path, legacy=self._legacy_ledger_path)
        current = self._load_state(self.ledger_path)
        current_generation = int(current.get("ledger_generation", -1))
        current_sha = self._active_state_sha256(current)
        if (current_generation != receipt["active_generation"]
                or current_sha != receipt["active_state_sha256"]):
            raise EntryReservationError(
                "durable entry reservation state is not the receipt-bound active generation")
        for field in ("migration_origin_sha256", "ledger_generation", "previous_state_sha256"):
            if state.get(field) != current.get(field):
                raise EntryReservationError(
                    f"entry reservation mutation changed immutable lineage field {field}")
        next_state = dict(state)
        next_state["ledger_generation"] = current_generation + 1
        next_state["previous_state_sha256"] = current_sha
        self._write_state_unbound(next_state)
        written = self._load_state(self.ledger_path)
        next_sha = self._active_state_sha256(written)
        next_receipt = dict(receipt)
        next_receipt["active_generation"] = current_generation + 1
        next_receipt["active_state_sha256"] = next_sha
        next_receipt["updated_at"] = _finite(self.clock(), name="clock")
        self._write_json_atomic(self.migration_receipt_path, next_receipt)



        state["ledger_generation"] = current_generation + 1
        state["previous_state_sha256"] = current_sha

    def _read_migration_receipt(self, path: Path, *, legacy: Path) -> dict:
        try:
            with path.open() as handle:
                receipt = json.load(handle)
        except Exception as exc:
            raise EntryReservationError(
                f"entry reservation migration receipt unreadable: {path}: {exc}") from exc
        if not isinstance(receipt, dict) or receipt.get("schema") != MIGRATION_SCHEMA:
            raise EntryReservationError(
                f"entry reservation migration receipt has an unsupported schema: {path}")
        expected_source = str(legacy)
        expected_destination = str(self.ledger_path)
        if receipt.get("source_path") != expected_source:
            raise EntryReservationError(
                "entry reservation migration receipt source path mismatch")
        if receipt.get("destination_path") != expected_destination:
            raise EntryReservationError(
                "entry reservation migration receipt destination path mismatch")
        source_sha = _required_version(
            receipt.get("source_evidence_sha256"),
            name="source_evidence_sha256", length=64)
        destination_sha = _required_version(
            receipt.get("destination_initial_evidence_sha256"),
            name="destination_initial_evidence_sha256", length=64)
        if source_sha != destination_sha:
            raise EntryReservationError(
                "entry reservation migration receipt does not bind identical source/destination evidence")
        _finite(receipt.get("migrated_at"), name="migrated_at")
        if receipt.get("legacy_entry_processes_stopped") is not True:
            raise EntryReservationError(
                "entry reservation migration receipt lacks the service-stop attestation")
        source_present = _bool(
            receipt.get("legacy_source_present"), name="legacy_source_present")
        source_was_absent = _bool(
            receipt.get("legacy_was_absent"), name="legacy_was_absent")
        if source_was_absent is not (not source_present):
            raise EntryReservationError(
                "entry reservation migration receipt has contradictory legacy presence evidence")
        if source_present:
            if receipt.get("legacy_source_backup_path") != str(self.legacy_backup_path):
                raise EntryReservationError(
                    "entry reservation migration receipt backup path mismatch")
            source_bytes_sha = _required_version(
                receipt.get("legacy_source_bytes_sha256"),
                name="legacy_source_bytes_sha256", length=64)
            try:
                backup_bytes = self.legacy_backup_path.read_bytes()
            except OSError as exc:
                raise EntryReservationError(
                    f"legacy source backup unreadable: {self.legacy_backup_path}: {exc}") from exc
            if hashlib.sha256(backup_bytes).hexdigest() != source_bytes_sha:
                raise EntryReservationError(
                    "legacy source backup does not match the migration receipt")
            receipt["legacy_source_bytes_sha256"] = source_bytes_sha
        elif (receipt.get("legacy_source_backup_path") not in (None, "")
              or receipt.get("legacy_source_bytes_sha256") not in (None, "")):
            raise EntryReservationError(
                "absent legacy source may not claim backup bytes")
        active_sha = _required_version(
            receipt.get("active_state_sha256"), name="active_state_sha256", length=64)
        active_generation = receipt.get("active_generation")
        if (isinstance(active_generation, bool) or not isinstance(active_generation, int)
                or active_generation < 0):
            raise EntryReservationError("active_generation must be a non-negative integer")
        receipt["source_evidence_sha256"] = source_sha
        receipt["destination_initial_evidence_sha256"] = destination_sha
        receipt["active_state_sha256"] = active_sha
        receipt["active_generation"] = active_generation
        return receipt

    def _read_legacy_fence(self, path: Path) -> dict:
        try:
            with path.open() as handle:
                fence = json.load(handle)
        except Exception as exc:
            raise EntryReservationError(
                f"legacy entry reservation fence unreadable: {path}: {exc}") from exc
        if not isinstance(fence, dict) or fence.get("schema") != LEGACY_FENCE_SCHEMA:
            raise EntryReservationError(
                "legacy entry reservation path is not fenced against pre-migration processes")
        if fence.get("destination_path") != str(self.ledger_path):
            raise EntryReservationError("legacy entry reservation fence destination mismatch")
        if fence.get("receipt_path") != str(self.migration_receipt_path):
            raise EntryReservationError("legacy entry reservation fence receipt mismatch")
        fence["source_evidence_sha256"] = _required_version(
            fence.get("source_evidence_sha256"),
            name="legacy_fence.source_evidence_sha256", length=64)
        _finite(fence.get("fenced_at"), name="legacy_fence.fenced_at")
        source_present = _bool(
            fence.get("legacy_source_present"), name="legacy_fence.legacy_source_present")
        if source_present:
            if fence.get("legacy_source_backup_path") != str(self.legacy_backup_path):
                raise EntryReservationError("legacy entry reservation fence backup path mismatch")
            fence["legacy_source_bytes_sha256"] = _required_version(
                fence.get("legacy_source_bytes_sha256"),
                name="legacy_fence.source_bytes_sha256", length=64)
        elif (fence.get("legacy_source_backup_path") not in (None, "")
              or fence.get("legacy_source_bytes_sha256") not in (None, "")):
            raise EntryReservationError("absent legacy fence may not claim backup bytes")
        return fence

    def _legacy_fence_payload(self, binding: dict, *, receipt: Path) -> dict:
        """Public API contract; production-derived narrative omitted."""
        source_present = binding["legacy_source_present"]
        return {
            "schema": LEGACY_FENCE_SCHEMA,
            "source_evidence_sha256": binding["source_evidence_sha256"],
            "destination_path": str(self.ledger_path),
            "receipt_path": str(receipt),
            "fenced_at": _finite(self.clock(), name="clock"),
            "legacy_source_present": source_present,
            "legacy_source_backup_path": (
                str(self.legacy_backup_path) if source_present else None),
            "legacy_source_bytes_sha256": binding.get("legacy_source_bytes_sha256"),
        }

    def _validate_active_binding_locked(self, receipt: dict, *, repair_forward: bool) -> dict:
        state = self._load_state(self.ledger_path)
        if state.get("migration_origin_sha256") != receipt["source_evidence_sha256"]:
            raise EntryReservationError(
                "durable entry reservation ledger is not bound to the migration origin")
        generation = int(state.get("ledger_generation", -1))
        active_sha = self._active_state_sha256(state)
        if (generation == receipt["active_generation"]
                and active_sha == receipt["active_state_sha256"]):
            return state



        if (repair_forward
                and generation == receipt["active_generation"] + 1
                and state.get("previous_state_sha256") == receipt["active_state_sha256"]):
            repaired = dict(receipt)
            repaired["active_generation"] = generation
            repaired["active_state_sha256"] = active_sha
            repaired["updated_at"] = _finite(self.clock(), name="clock")
            self._write_json_atomic(self.migration_receipt_path, repaired)
            return state
        raise EntryReservationError(
            "durable entry reservation ledger does not match the receipt-bound active generation "
            "(possible rollback or replacement)")

    def _validate_completed_migration_locked(self, *, legacy: Path, receipt: Path) -> None:
        if not receipt.exists():
            raise EntryReservationError(
                "durable entry reservation ledger has no migration receipt")
        bound = self._read_migration_receipt(receipt, legacy=legacy)
        if not self.ledger_path.exists():
            raise EntryReservationError(
                "durable entry reservation ledger is missing after completed migration")
        if not legacy.exists():
            raise EntryReservationError(
                "legacy entry reservation fence is missing after migration")
        fence = self._read_legacy_fence(legacy)
        if fence["source_evidence_sha256"] != bound["source_evidence_sha256"]:
            raise EntryReservationError(
                "legacy entry reservation fence does not match the migration receipt")
        if (fence["legacy_source_present"] != bound["legacy_source_present"]
                or fence.get("legacy_source_bytes_sha256")
                != bound.get("legacy_source_bytes_sha256")):
            raise EntryReservationError(
                "legacy entry reservation fence backup evidence does not match the receipt")
        self._validate_active_binding_locked(bound, repair_forward=True)

    def _require_default_storage_ready(self) -> None:
        with _exclusive_lock(self.lock_path, self.lock_timeout_seconds,
                             label="durable entry reservation startup"):
            with _exclusive_lock(self._legacy_lock_path, self.lock_timeout_seconds,
                                 label="legacy entry reservation startup"):
                self._validate_completed_migration_locked(
                    legacy=self._legacy_ledger_path, receipt=self.migration_receipt_path)

    def _bootstrap_default_storage(self, *, legacy_entry_processes_stopped: bool,
                                   absent_legacy_verified_empty: bool,
                                   legacy_ledger=None, legacy_lock=None,
                                   receipt_path=None) -> None:
        """Public API contract; production-derived narrative omitted."""
        if legacy_entry_processes_stopped is not True:
            raise EntryReservationError(
                "bootstrap requires legacy_entry_processes_stopped=True")
        legacy = self._legacy_ledger_path if legacy_ledger is None else Path(legacy_ledger)
        legacy_lock = self._legacy_lock_path if legacy_lock is None else Path(legacy_lock)
        receipt = self.migration_receipt_path if receipt_path is None else Path(receipt_path)
        if legacy == self.ledger_path:
            raise EntryReservationError("legacy and durable entry reservation ledgers are identical")
        if receipt in (legacy, self.ledger_path, self.lock_path, legacy_lock):
            raise EntryReservationError("entry reservation migration receipt path collides with state")

        with _exclusive_lock(self.lock_path, self.lock_timeout_seconds,
                             label="durable entry reservation migration"):
            with _exclusive_lock(legacy_lock, self.lock_timeout_seconds,
                                 label="legacy entry reservation migration"):
                if receipt.exists():




                    if not legacy.exists():
                        bound = self._read_migration_receipt(receipt, legacy=legacy)
                        if not self.ledger_path.exists():
                            raise EntryReservationError(
                                "durable entry reservation ledger is missing after completed "
                                "migration")
                        self._validate_active_binding_locked(bound, repair_forward=True)
                        self._write_json_atomic(
                            legacy, self._legacy_fence_payload(bound, receipt=receipt))
                    self._validate_completed_migration_locked(legacy=legacy, receipt=receipt)
                    return

                source_was_absent = not legacy.exists()
                source_state = None
                source_sha = None
                source_bytes_sha = None
                if not source_was_absent:
                    try:
                        fence = self._read_legacy_fence(legacy)
                    except EntryReservationError:
                        source_bytes = legacy.read_bytes()
                        source_state = self._load_state(legacy)
                        source_sha = self._state_evidence_sha256(source_state)
                        source_bytes_sha = hashlib.sha256(source_bytes).hexdigest()
                        self._write_bytes_create_only(self.legacy_backup_path, source_bytes)
                    else:
                        source_sha = fence["source_evidence_sha256"]
                        source_was_absent = not fence["legacy_source_present"]
                        source_bytes_sha = fence.get("legacy_source_bytes_sha256")
                        if not source_was_absent:
                            try:
                                backup_bytes = self.legacy_backup_path.read_bytes()
                            except OSError as exc:
                                raise EntryReservationError(
                                    f"legacy source backup unreadable: "
                                    f"{self.legacy_backup_path}: {exc}") from exc
                            if hashlib.sha256(backup_bytes).hexdigest() != source_bytes_sha:
                                raise EntryReservationError(
                                    "legacy source backup does not match the recovery fence")
                elif absent_legacy_verified_empty is not True:
                    raise EntryReservationError(
                        "legacy ledger is absent; bootstrap requires "
                        "absent_legacy_verified_empty=True after broker reconciliation")
                else:
                    if self.legacy_backup_path.exists():
                        raise EntryReservationError(
                            "legacy ledger is absent but a prior source backup exists; refusing "
                            "to overwrite contradictory migration evidence")
                    source_state = self._empty_state()
                    source_sha = self._state_evidence_sha256(source_state)

                if self.ledger_path.exists():
                    destination_state = self._load_state(self.ledger_path)
                    destination_origin = destination_state.get("migration_origin_sha256")
                    if destination_origin is None:


                        if self._state_evidence_sha256(destination_state) != source_sha:
                            raise EntryReservationError(
                                "existing durable and legacy reservation evidence differ")
                        destination_state["migration_origin_sha256"] = source_sha
                        destination_state["ledger_generation"] = 0
                        destination_state["previous_state_sha256"] = source_sha
                        self._write_state_unbound(destination_state)
                        destination_state = self._load_state(self.ledger_path)
                    elif destination_origin != source_sha:
                        raise EntryReservationError(
                            "existing durable ledger is not bound to the legacy evidence")
                    if (destination_state.get("ledger_generation") != 0
                            or destination_state.get("previous_state_sha256") != source_sha):
                        raise EntryReservationError(
                            "unreceipted durable ledger is not an initial migration generation")
                else:
                    if source_state is None:
                        raise EntryReservationError(
                            "fenced legacy source exists but the durable migration copy is missing")
                    destination_state = dict(source_state)
                    destination_state["migration_origin_sha256"] = source_sha
                    destination_state["ledger_generation"] = 0
                    destination_state["previous_state_sha256"] = source_sha
                    self._write_state_unbound(destination_state)
                    destination_state = self._load_state(self.ledger_path)
                destination_sha = self._state_evidence_sha256(destination_state)
                if destination_sha != source_sha:
                    raise EntryReservationError(
                        "durable entry reservation migration verification failed")
                active_sha = self._active_state_sha256(destination_state)

                fence_binding = {
                    "source_evidence_sha256": source_sha,
                    "legacy_source_present": not source_was_absent,
                    "legacy_source_bytes_sha256": source_bytes_sha,
                }
                self._write_json_atomic(
                    legacy, self._legacy_fence_payload(fence_binding, receipt=receipt))

                migration_receipt = {
                    "schema": MIGRATION_SCHEMA,
                    "source_path": str(legacy),
                    "destination_path": str(self.ledger_path),
                    "source_evidence_sha256": source_sha,
                    "destination_initial_evidence_sha256": source_sha,
                    "migrated_at": _finite(self.clock(), name="clock"),
                    "legacy_entry_processes_stopped": True,
                    "legacy_was_absent": source_was_absent,
                    "legacy_source_present": not source_was_absent,
                    "legacy_source_backup_path": (
                        str(self.legacy_backup_path) if not source_was_absent else None),
                    "legacy_source_bytes_sha256": source_bytes_sha,
                    "active_generation": 0,
                    "active_state_sha256": active_sha,
                }
                self._write_json_atomic(receipt, migration_receipt)
                self._validate_completed_migration_locked(legacy=legacy, receipt=receipt)

    @staticmethod
    def _sum(reservations: Mapping[str, dict]) -> float:
        return round(sum(float(row["collateral_usd"]) for row in reservations.values()), 2)

    @staticmethod
    def _capital_sum(reservations: Mapping[str, dict]) -> float:
        return round(sum(float(row.get("capital_usd", row["collateral_usd"]))
                         for row in reservations.values()), 2)

    def _reconcile_locked(self, state: dict, *, now: float, broker_deployed_usd: float,
                          visible_order_refs: frozenset[str]):
        reservations = state["reservations"]
        expired = []
        visible_candidates = []
        for ref, row in list(reservations.items()):
            if float(row["expires_at"]) <= now:
                expired.append(ref)
                continue






            if ref in visible_order_refs and row.get("side", "credit") == "credit":
                visible_candidates.append(ref)
        for ref in expired:
            reservations.pop(ref, None)



        visible_total = sum(float(reservations[ref]["collateral_usd"])
                            for ref in visible_candidates if ref in reservations)
        visible = []
        if broker_deployed_usd + _EPS >= visible_total:
            for ref in visible_candidates:
                if ref in reservations:
                    reservations.pop(ref)
                    visible.append(ref)
        return tuple(sorted(visible)), tuple(sorted(expired))



    @staticmethod
    def _reflected_debits(reservations: Mapping[str, dict], n: dict) -> tuple[dict, ...]:
        """Public API contract; production-derived narrative omitted."""
        matched = []
        for proof in n.get("debit_reflections", ()):
            row = reservations.get(proof["order_ref"])
            if row is None or row.get("side") != "debit":
                continue
            if (row["con_id"] != proof["con_id"]
                    or tuple(sorted(row.get("leg_con_ids") or ())) != proof["leg_con_ids"]
                    or row.get("contracts") != proof["contracts"]
                    or row.get("symbol") != proof["symbol"]
                    or row.get("sector_cluster") != proof["sector_cluster"]):
                continue


            if any(other_ref != proof["order_ref"]
                   and set(proof["leg_con_ids"]).intersection(other.get("leg_con_ids") or ())
                   for other_ref, other in reservations.items()):
                continue
            matched.append(proof)


        if (sum(p["capital_usd"] for p in matched) > n["deployed_agg"] + _EPS
                or len(matched) > n["open_count"]):
            return ()
        return tuple(matched)

    def _normalize(self, dims: Mapping[str, Any]) -> dict:
        """Public API contract; production-derived narrative omitted."""
        n = {}
        n["ref"] = _order_ref(dims["order_ref"])
        n["con_id"] = _con_id(dims["con_id"])
        n["envelope_id"] = _text(dims["envelope_id"], limit=128)
        n["code_version"] = _required_version(
            dims["code_version"], name="code_version", length=40)
        n["policy_version"] = _required_version(
            dims["policy_version"], name="policy_version", length=64)
        n["legs"] = _con_ids(dims["leg_con_ids"], name="leg_con_ids")
        if not n["legs"]:
            raise EntryReservationError("leg_con_ids must name at least one contract")
        if n["con_id"] not in n["legs"]:
            raise EntryReservationError("primary con_id must be present in leg_con_ids")
        n["symbol"] = _text(dims["symbol"], limit=32, upper=True)
        if not n["symbol"]:
            raise EntryReservationError("symbol is required")
        n["sector_cluster"] = _text(dims["sector_cluster"], limit=64, upper=True) or n["symbol"]
        n["structure"] = _text(dims["structure"], limit=64)
        side = _text(dims["side"], limit=8).lower()
        if side not in ("debit", "credit"):
            raise EntryReservationError(f"side must be 'debit' or 'credit': {dims['side']!r}")
        n["side"] = side
        n["capital"] = round(_finite(dims["capital_usd"], name="capital_usd", positive=True), 2)
        n["collateral"] = round(
            _finite(dims["collateral_usd"], name="collateral_usd", positive=True), 2)
        if n["capital"] <= 0 or n["collateral"] <= 0:
            raise EntryReservationError("capital_usd/collateral_usd round to zero cents")
        n["contracts"] = int(_finite(dims["contracts"], name="contracts", positive=True))
        n["net_liq"] = _finite(dims["net_liq"], name="net_liq", positive=True)
        n["available"] = _finite(dims["available_funds"], name="available_funds",
                                 non_negative=True)
        n["deployed"] = _finite(dims["broker_deployed_usd"], name="broker_deployed_usd",
                                non_negative=True)
        n["readable"] = _bool(dims["observation_readable"], name="observation_readable")
        n["observed_at"] = _finite(dims["observed_at_monotonic"], name="observed_at_monotonic")
        n["max_age"] = _finite(dims["max_observation_age_s"], name="max_observation_age_s",
                               positive=True)
        n["open_count"] = int(_finite(dims["open_position_count"], name="open_position_count",
                                      non_negative=True))
        n["max_concurrent"] = int(_finite(dims["max_concurrent"], name="max_concurrent",
                                          positive=True))
        n["name_agg"] = _finite(dims["name_aggregate_usd"], name="name_aggregate_usd",
                                non_negative=True)
        n["name_cap"] = _optional_cap(dims["name_cap_usd"], name="name_cap_usd")
        n["sector_agg"] = _finite(dims["sector_aggregate_usd"], name="sector_aggregate_usd",
                                  non_negative=True)
        n["sector_cap"] = _optional_cap(dims["sector_cap_usd"], name="sector_cap_usd")
        n["deployed_agg"] = _finite(dims["deployed_aggregate_usd"],
                                    name="deployed_aggregate_usd", non_negative=True)
        n["deployed_cap"] = _optional_cap(dims["deployed_cap_usd"], name="deployed_cap_usd")
        n["day_orders"] = int(_finite(dims["day_orders"], name="day_orders", non_negative=True))
        n["max_day_orders"] = _optional_int_cap(dims["max_orders_per_day"],
                                                name="max_orders_per_day")
        n["day_notional"] = _finite(dims["day_notional"], name="day_notional", non_negative=True)
        n["max_day_notional"] = _optional_cap(dims["max_notional_per_day"],
                                              name="max_notional_per_day")
        n["campaign_con_ids"] = frozenset(
            _con_ids(dims["open_campaign_con_ids"], name="open_campaign_con_ids"))
        n["open_campaigns"] = _open_campaign_records(dims["open_campaigns"])
        conflict_symbols = dims["campaign_conflict_symbols"]
        if (isinstance(conflict_symbols, (str, bytes))
                or not isinstance(conflict_symbols, (list, tuple, set, frozenset))):
            raise EntryReservationError(
                "campaign_conflict_symbols must be an explicit symbol collection")
        normalized_conflicts = set()
        for value in conflict_symbols:
            symbol = _text(value, limit=32, upper=True)
            if not symbol:
                raise EntryReservationError(
                    "campaign_conflict_symbols contains an invalid symbol")
            normalized_conflicts.add(symbol)
        n["campaign_conflict_symbols"] = frozenset(normalized_conflicts)
        campaign_record_legs = {
            leg for campaign in n["open_campaigns"] for leg in campaign["leg_con_ids"]}
        if not campaign_record_legs.issubset(n["campaign_con_ids"]):
            raise EntryReservationError(
                "open_campaign_con_ids must include every exact leg in open_campaigns")
        n["campaign_add"] = _campaign_add_document(dims["campaign_add_intent"])
        if n["campaign_add"] is not None:
            add = n["campaign_add"]
            if (add["primary_con_id"] != n["con_id"]
                    or tuple(add["leg_con_ids"]) != tuple(n["legs"])):
                raise EntryReservationError(
                    "campaign add identity conflicts with the final candidate contract")
        n["final_contract"] = _json_safe(
            dims["final_contract"], name="final_contract")
        n["structured_intent"] = _json_safe(
            dims["structured_intent"], name="structured_intent")
        if not isinstance(n["final_contract"], dict) or not n["final_contract"]:
            raise EntryReservationError("final_contract must be a non-empty mapping")
        if not isinstance(n["structured_intent"], dict) or not n["structured_intent"]:
            raise EntryReservationError("structured_intent must be a non-empty mapping")
        final = n["final_contract"]
        final_legs = [final.get("long_con_id")]
        if final.get("short_con_id") is not None:
            final_legs.append(final.get("short_con_id"))
        try:
            final_legs = _con_ids(final_legs, name="final_contract legs")
            final_qty = int(_finite(final.get("quantity"), name="final_contract.quantity",
                                    positive=True))
        except EntryReservationError:
            raise
        if (final_legs != n["legs"] or _con_id(final.get("long_con_id")) != n["con_id"]
                or final_qty != n["contracts"]
                or _text(final.get("underlying"), limit=32, upper=True) != n["symbol"]
                or _text(final.get("side"), limit=8).lower() != n["side"]
                or _text(final.get("structure"), limit=64) != n["structure"]):
            raise EntryReservationError(
                "final_contract conflicts with the exact admission contract/quantity/intent")





        final_limit = _finite(final.get("limit"), name="final_contract.limit", positive=True)
        final_max_loss = round(_finite(
            final.get("max_loss_usd"), name="final_contract.max_loss_usd", positive=True), 2)
        executable_total = round(final_limit * 100.0 * final_qty, 2)
        if n["side"] == "debit":
            if (abs(executable_total - n["capital"]) > 0.01
                    or abs(final_max_loss - n["capital"]) > 0.01
                    or abs(n["collateral"] - n["capital"]) > 0.01):
                raise EntryReservationError(
                    "final_contract debit economics conflict with admission capital")
        else:
            final_collateral = round(_finite(
                final.get("collateral_usd"), name="final_contract.collateral_usd",
                positive=True), 2)
            final_credit = round(_finite(
                final.get("net_credit_usd"), name="final_contract.net_credit_usd",
                positive=True), 2)
            if (abs(final_collateral - n["capital"]) > 0.01
                    or abs(final_collateral - n["collateral"]) > 0.01
                    or abs(executable_total - final_credit) > 0.01
                    or abs(final_collateral - final_credit - final_max_loss) > 0.01):
                raise EntryReservationError(
                    "final_contract credit economics conflict with admission capital")
        stated = n["structured_intent"]
        if stated.get("schema") != "structured_entry_intent.v1":
            raise EntryReservationError("structured_intent schema must be structured_entry_intent.v1")
        if (stated.get("final_contract") != final
                or _text(stated.get("underlying"), limit=32, upper=True) != n["symbol"]
                or _text(stated.get("side"), limit=8).lower() != n["side"]
                or _text(stated.get("structure"), limit=64) != n["structure"]):
            raise EntryReservationError(
                "structured_intent is not bound to the resolved final contract")
        if _campaign_add_document(stated.get("campaign_add_intent")) != n["campaign_add"]:
            raise EntryReservationError(
                "structured_intent campaign add differs from admission authority")
        stage_a = stated.get("stage_a_intent")
        stage_b = stated.get("stage_b_candidate")
        if (stage_a is None) != (stage_b is None):
            raise EntryReservationError("structured_intent must carry both Stage A and Stage B or neither")
        if stage_a is not None:
            if not isinstance(stage_a, Mapping) or not isinstance(stage_b, Mapping):
                raise EntryReservationError("structured Stage A/B payloads must be mappings")
            try:
                candidate_legs = _con_ids(
                    [leg.get("con_id") for leg in stage_b.get("legs", ())],
                    name="stage_b_candidate.legs")
            except AttributeError as exc:
                raise EntryReservationError("stage_b_candidate legs are malformed") from exc
            if (candidate_legs != n["legs"]
                    or _text(stage_a.get("underlying"), limit=32, upper=True) != n["symbol"]
                    or _text(stage_b.get("underlying"), limit=32, upper=True) != n["symbol"]
                    or _text(stage_a.get("side"), limit=8).lower() != n["side"]
                    or _text(stage_b.get("side"), limit=8).lower() != n["side"]
                    or _text(stage_a.get("structure"), limit=64) != n["structure"]
                    or _text(stage_b.get("structure"), limit=64) != n["structure"]
                    or stated.get("stage_a_intent_id") != stage_b.get("intent_id")
                    or stated.get("stage_b_candidate_id") != stage_b.get("candidate_id")):
                raise EntryReservationError(
                    "Stage A/B structured intent conflicts with the resolved candidate")
        n["markers_clear"] = _bool(dims["markers_clear"], name="markers_clear")
        n["refs"] = _visible_refs(dims["visible_order_refs"])
        n["debit_reflections"] = _debit_reflection_proofs(
            dims.get("reflected_debit_positions", ()))
        return n

    def _decide_locked(self, state: dict, n: dict, *, now: float) -> tuple[ReservationDecision,
                                                                          Optional[dict]]:
        """Public API contract; production-derived narrative omitted."""
        ref, cid = n["ref"], n["con_id"]

        def _refuse(status, reasons, *, outstanding=0.0):
            return ReservationDecision(
                False, False, status, tuple(reasons), ref, cid, n["collateral"],
                outstanding, n["deployed"] + outstanding, n["available"] - outstanding,
                side=n["side"], symbol=n["symbol"], capital_usd=n["capital"],
                effective_open_positions=n["open_count"] + len(state["reservations"]),
                reserved_slots=len(state["reservations"])), None





        if not n["readable"]:
            return _refuse("observation_unreadable", (
                "the broker observation backing this admission could not be verified "
                "(refusing: an unreadable book is never treated as an empty one)",))




        age = time.monotonic() - n["observed_at"]
        if age > n["max_age"] or age < -1.0:
            return _refuse("stale_observation", (
                f"broker observation is {age:.1f}s old (max {n['max_age']:.1f}s)",))



        if not n["markers_clear"]:
            return _refuse("markers_set", ("a halt marker was set before this admission",))

        removed_visible, _removed_expired = self._reconcile_locked(
            state, now=now, broker_deployed_usd=n["deployed"], visible_order_refs=n["refs"])
        reservations = state["reservations"]



        if ref in n["refs"] or ref in removed_visible:
            return ReservationDecision(
                True, False, "broker_visible", (), ref, cid, n["collateral"],
                self._sum(reservations), n["deployed"], n["available"],
                side=n["side"], symbol=n["symbol"], capital_usd=n["capital"],
                reserved_slots=len(reservations),
                effective_open_positions=n["open_count"] + len(reservations)), None







        orphan_intents = {prior_ref: prior for prior_ref, prior in
                          state.get("intents", {}).items()
                          if prior_ref not in reservations}
        if ref in orphan_intents:
            return _refuse("intent_unresolved", (
                "order_ref already has an unresolved durable entry intent; broker outcome must "
                "be reconciled before another transmission",),
                outstanding=self._sum(reservations))
        requested_legs = set(n["legs"])
        for prior_ref, prior in orphan_intents.items():
            prior_legs = set(int(x) for x in (prior.get("leg_con_ids") or ()))
            overlap = sorted(requested_legs & prior_legs)
            if overlap:
                return _refuse("intent_unresolved", (
                    "contract(s) %s remain bound to unresolved intent %s; refusing a possible "
                    "duplicate entry" % (", ".join(str(x) for x in overlap), prior_ref),),
                    outstanding=self._sum(reservations))

        existing = reservations.get(ref)
        if existing is not None:
            outstanding = self._sum(reservations)
            if (int(existing["con_id"]) != cid
                    or abs(float(existing["collateral_usd"]) - n["collateral"]) > 0.01):
                return _refuse("conflict",
                               ("order_ref already reserves different contract/collateral",),
                               outstanding=outstanding)
            return ReservationDecision(
                True, False, "already_reserved", (), ref, cid, n["collateral"],
                outstanding, n["deployed"] + outstanding, n["available"] - outstanding,
                side=n["side"], symbol=n["symbol"], capital_usd=n["capital"],
                reserved_slots=len(reservations),
                effective_open_positions=n["open_count"] + len(reservations)), None


        outstanding = self._sum(reservations)
        outstanding_credit = round(sum(
            float(r["collateral_usd"]) for r in reservations.values()
            if r.get("side", "credit") == "credit"), 2)
        reflected = self._reflected_debits(reservations, n)
        reflected_refs = {p["order_ref"] for p in reflected}
        unreflected_positions = {r: row for r, row in reservations.items()
                                 if r not in reflected_refs}
        outstanding_capital = self._capital_sum(unreflected_positions)
        reserved_slots = sum(
            1 for row in unreflected_positions.values()
            if not row.get("campaign_add_intent"))
        reflection_audit = dict(
            reflected_debit_order_refs=tuple(sorted(reflected_refs)),
            reflected_debit_capital_usd=self._capital_sum(
                {r: reservations[r] for r in reflected_refs}),
            reflected_debit_actual_usd=round(sum(p["capital_usd"] for p in reflected), 2))
        effective_deployed = n["deployed"] + outstanding_credit


        effective_available = n["available"] - outstanding
        name_proofs = [p for p in reflected if p["symbol"] == n["symbol"]
                       and p["name_counted_usd"] > 0]
        sector_proofs = [p for p in reflected if p["sector_cluster"] == n["sector_cluster"]
                         and p["sector_counted_usd"] > 0]
        name_refs = ({p["order_ref"] for p in name_proofs}
                     if sum(p["name_counted_usd"] for p in name_proofs) <= n["name_agg"] + _EPS
                     else set())
        sector_refs = ({p["order_ref"] for p in sector_proofs}
                       if sum(p["sector_counted_usd"] for p in sector_proofs)
                       <= n["sector_agg"] + _EPS else set())


        name_reserved = round(sum(
            float(r.get("capital_usd", r["collateral_usd"])) for ref, r in reservations.items()
            if ref not in name_refs and r.get("symbol", "") in ("", n["symbol"])), 2)
        sector_reserved = round(sum(
            float(r.get("capital_usd", r["collateral_usd"])) for ref, r in reservations.items()
            if ref not in sector_refs and r.get("sector_cluster", "") in ("", n["sector_cluster"])), 2)
        untracked_orders = sum(1 for r in reservations.values()
                               if not r.get("throttle_recorded", True))
        untracked_notional = round(sum(
            float(r.get("capital_usd", r["collateral_usd"])) for r in reservations.values()
            if not r.get("throttle_recorded", True)), 2)
        reserved_legs = set()
        for row in reservations.values():
            reserved_legs.update(int(x) for x in row.get("leg_con_ids") or ())

        status = ""
        reasons: list[str] = []

        def _fail(new_status, message):
            nonlocal status
            status = status or new_status
            reasons.append(message)




        if n["symbol"] in n["campaign_conflict_symbols"]:
            _fail("campaign_conflict_refused",
                  "%s has an active journal campaign-authority conflict" % n["symbol"])





        candidate_legs = set(n["legs"])
        open_overlap = sorted(candidate_legs & set(n["campaign_con_ids"]))
        reservation_overlap_refs = {
            prior_ref for prior_ref, prior in reservations.items()
            if candidate_legs & set(int(x) for x in prior.get("leg_con_ids") or ())}
        add = n.get("campaign_add")
        add_campaign = None
        if add is None:
            overlap = sorted(candidate_legs & (set(n["campaign_con_ids"]) | reserved_legs))
            if overlap:
                _fail("campaign_overlap_refused",
                      "contract(s) %s already carry an open campaign or pending admission; "
                      "an explicit bounded campaign-add intent is required"
                      % ", ".join(str(x) for x in overlap))
        else:
            matching = [c for c in n.get("open_campaigns", ())
                        if c["campaign_id"] == add["campaign_id"]]
            if len(matching) != 1:
                _fail("campaign_add_target_refused",
                      "campaign add target is absent or ambiguous in the fresh position book")
            else:
                add_campaign = matching[0]
                if (add_campaign["primary_con_id"] != cid
                        or tuple(add_campaign["leg_con_ids"]) != tuple(n["legs"])
                        or add_campaign["symbol"] != n["symbol"]
                        or set(open_overlap) != candidate_legs):
                    _fail("campaign_add_target_refused",
                          "campaign add does not exactly match the live campaign contract/legs/name")
                if not add_campaign["risk_known"]:
                    _fail("campaign_add_target_refused",
                          "live campaign risk is unknown; bounded scale-in cannot be proved")
            compatible_pending = []
            for prior_ref in sorted(reservation_overlap_refs):
                prior = reservations[prior_ref]
                try:
                    prior_add = _campaign_add_document(prior.get("campaign_add_intent"))
                except EntryReservationError:
                    prior_add = None
                if (prior_add is None
                        or prior_add["campaign_id"] != add["campaign_id"]
                        or tuple(prior_add["leg_con_ids"]) != tuple(n["legs"])):
                    _fail("campaign_overlap_refused",
                          f"pending admission {prior_ref} overlaps this campaign without the same "
                          "explicit add authority")
                else:
                    compatible_pending.append((prior, prior_add))
            if add_campaign is not None and add_campaign["risk_known"]:
                post_contracts = (add_campaign["contracts"]
                                  + sum(int(row.get("contracts", 0) or 0)
                                        for row, _ in compatible_pending)
                                  + n["contracts"])
                post_risk = round(
                    add_campaign["risk_usd"]
                    + sum(float(row.get("capital_usd", 0.0) or 0.0)
                          for row, _ in compatible_pending)
                    + n["capital"], 2)
                qty_cap = min([add["max_campaign_contracts"]]
                              + [doc["max_campaign_contracts"]
                                 for _, doc in compatible_pending])
                risk_cap = min([add["max_campaign_risk_usd"]]
                               + [doc["max_campaign_risk_usd"]
                                  for _, doc in compatible_pending])
                if post_contracts > qty_cap:
                    _fail("campaign_add_bound_refused",
                          f"campaign quantity {post_contracts} would exceed explicit bound {qty_cap}")
                if post_risk > risk_cap + _EPS:
                    _fail("campaign_add_bound_refused",
                          f"campaign risk ${post_risk:,.2f} would exceed explicit bound "
                          f"${risk_cap:,.2f}")




        if (SLOT_PREDICATE_ENABLED and add is None
                and n["open_count"] + reserved_slots >= n["max_concurrent"]):
            _fail("slot_refused",
                  f"at max concurrent positions ({n['open_count']} open + {reserved_slots} "
                  f"reserved / {n['max_concurrent']})")



        name_total = n["name_agg"] + name_reserved + n["capital"]
        sector_total = n["sector_agg"] + sector_reserved + n["capital"]
        if n["name_cap"] is not None:
            if name_total > n["name_cap"] + _EPS:
                _fail("name_refused",
                      f"single-name exposure ${name_total:,.2f} (broker ${n['name_agg']:,.2f} + "
                      f"pending ${name_reserved:,.2f} + this ${n['capital']:,.2f}) would exceed "
                      f"${n['name_cap']:,.2f}")
        if n["sector_cap"] is not None:
            if sector_total > n["sector_cap"] + _EPS:
                _fail("sector_refused",
                      f"sector '{n['sector_cluster']}' exposure ${sector_total:,.2f} (broker "
                      f"${n['sector_agg']:,.2f} + pending ${sector_reserved:,.2f} + this "
                      f"${n['capital']:,.2f}) would exceed ${n['sector_cap']:,.2f}")


        book_total = n["deployed_agg"] + outstanding_capital + n["capital"]
        if n["deployed_cap"] is not None:
            if book_total > n["deployed_cap"] + _EPS:
                _fail("deployed_refused",
                      f"deployed exposure ${book_total:,.2f} (broker ${n['deployed_agg']:,.2f} + "
                      f"pending ${outstanding_capital:,.2f} + this ${n['capital']:,.2f}) would "
                      f"exceed ${n['deployed_cap']:,.2f}")




        if n["max_day_orders"] is not None:
            if n["day_orders"] + untracked_orders + 1 > n["max_day_orders"]:
                _fail("throttle_refused",
                      f"daily order cap reached ({n['day_orders']} recorded + "
                      f"{untracked_orders} in flight >= {n['max_day_orders']})")
        if n["max_day_notional"] is not None:
            day_total = n["day_notional"] + untracked_notional + n["capital"]
            if day_total > n["max_day_notional"] + 1e-6:
                _fail("throttle_refused",
                      f"daily notional cap: ${day_total:,.2f} > ${n['max_day_notional']:,.2f}")



        if n["collateral"] > effective_available + _EPS:
            _fail("capacity_refused",
                  f"insufficient unreserved funds: need ${n['collateral']:,.2f}, "
                  f"available after pending reservations ${effective_available:,.2f}")
        cap = self.max_collateral_pct * n["net_liq"]
        if n["side"] == "credit" and effective_deployed + n["collateral"] > cap + _EPS:
            _fail("capacity_refused",
                  f"collateral cap: broker ${n['deployed']:,.2f} + pending credit "
                  f"${outstanding_credit:,.2f} "
                  f"+ this ${n['collateral']:,.2f} > {self.max_collateral_pct:.0%}-of-net-liq "
                  f"cap ${cap:,.2f}")

        if reasons:
            return ReservationDecision(
                False, False, status, tuple(reasons), ref, cid, n["collateral"],
                outstanding, effective_deployed, effective_available,
                side=n["side"], symbol=n["symbol"], capital_usd=n["capital"],
                effective_open_positions=n["open_count"] + reserved_slots,
                reserved_slots=reserved_slots, **reflection_audit), None

        receipt = {
            "schema": "entry_admission_receipt.v1",
            "admitted_at_epoch": now,
            "observation_age_seconds": round(time.monotonic() - n["observed_at"], 6),
            "final_contract": n.get("final_contract"),
            "structured_intent": n.get("structured_intent"),
            "net_liq_usd": n["net_liq"],
            "available_funds_usd": n["available"],
            "candidate": {"contracts": n["contracts"], "capital_usd": n["capital"],
                          "collateral_usd": n["collateral"]},
            "slots": {"open": n["open_count"], "pending": reserved_slots,
                      "post": n["open_count"] + reserved_slots + (0 if add else 1),
                      "cap": n["max_concurrent"]},
            "name": {"symbol": n["symbol"], "broker_usd": n["name_agg"],
                     "pending_usd": name_reserved, "this_usd": n["capital"],
                     "post_usd": round(name_total, 2), "cap_usd": n["name_cap"]},
            "sector": {"cluster": n["sector_cluster"], "broker_usd": n["sector_agg"],
                       "pending_usd": sector_reserved, "this_usd": n["capital"],
                       "post_usd": round(sector_total, 2), "cap_usd": n["sector_cap"]},
            "deployed": {"broker_usd": n["deployed_agg"],
                         "pending_usd": outstanding_capital, "this_usd": n["capital"],
                         "post_usd": round(book_total, 2), "cap_usd": n["deployed_cap"]},
            "campaign_add_intent": add,
        }
        for dimension in ("name", "sector", "deployed"):
            values = receipt[dimension]
            values["headroom_usd"] = (None if values["cap_usd"] is None else round(
                values["cap_usd"] - values["post_usd"], 2))

        row = {
            "order_ref": ref,
            "con_id": cid,
            "collateral_usd": n["collateral"],
            "created_at": now,
            "expires_at": now + self.ttl_seconds,
            "envelope_id": n["envelope_id"],
            "symbol": n["symbol"],
            "sector_cluster": n["sector_cluster"],
            "structure": n["structure"],
            "side": n["side"],
            "capital_usd": n["capital"],
            "contracts": n["contracts"],
            "leg_con_ids": list(n["legs"]),
            "observed_at": n["observed_at"],
            "throttle_recorded": False,


            "code_version": n.get("code_version", ""),
            "policy_version": n.get("policy_version", ""),
            "campaign_add_intent": add,
            "admission_receipt": receipt,
        }
        new_outstanding = round(outstanding + n["collateral"], 2)
        decision = ReservationDecision(
            True, True, "reserved", (), ref, cid, n["collateral"],
            new_outstanding, n["deployed"] + new_outstanding, n["available"] - new_outstanding,
            side=n["side"], symbol=n["symbol"], capital_usd=n["capital"],
            effective_open_positions=n["open_count"] + reserved_slots + (0 if add else 1),
            reserved_slots=reserved_slots + (0 if add else 1),
            admission_receipt=receipt, **reflection_audit)
        return decision, row



    def reserve(self, *, order_ref, con_id, collateral_usd,
                net_liq, available_funds, broker_deployed_usd,
                visible_order_refs: Optional[Iterable] = None) -> ReservationDecision:
        """Public API contract; production-derived narrative omitted."""
        ref = ""
        cid = 0
        collateral = deployed = net = available = 0.0
        try:
            ref = _order_ref(order_ref)
            cid = _con_id(con_id)
            collateral = round(
                _finite(collateral_usd, name="collateral_usd", positive=True), 2)
            if collateral <= 0:
                raise EntryReservationError("collateral_usd rounds to zero cents")
            net = _finite(net_liq, name="net_liq", positive=True)
            available = _finite(
                available_funds, name="available_funds", non_negative=True)
            deployed = _finite(
                broker_deployed_usd, name="broker_deployed_usd", non_negative=True)
            refs = _visible_refs(visible_order_refs)
            now = _finite(self.clock(), name="clock")
            with self._locked():
                state = self._load_state()
                before = json.dumps(state, sort_keys=True)
                decision, row = self._decide_locked(state, {
                    "ref": ref, "con_id": cid, "envelope_id": "", "legs": (cid,),
                    "symbol": "", "sector_cluster": "", "structure": "", "side": "credit",
                    "capital": collateral, "collateral": collateral, "contracts": 1,
                    "net_liq": net, "available": available, "deployed": deployed,
                    "readable": True, "observed_at": time.monotonic(),
                    "max_age": float("inf"),
                    "open_count": 0, "max_concurrent": 1 << 30,
                    "name_agg": 0.0, "name_cap": None,
                    "sector_agg": 0.0, "sector_cap": None,
                    "deployed_agg": 0.0, "deployed_cap": None,
                    "day_orders": 0, "max_day_orders": None,
                    "day_notional": 0.0, "max_day_notional": None,
                    "campaign_con_ids": frozenset(), "markers_clear": True, "refs": refs,
                    "overlap_enabled": False, "campaign_conflict_symbols": frozenset(),
                }, now=now)
                if row is not None:



                    row["symbol"] = ""
                    row["sector_cluster"] = ""
                    state["reservations"][ref] = row
                    self._write_state(state)
                elif json.dumps(state, sort_keys=True) != before:
                    self._write_state(state)
                return decision
        except Exception as exc:
            return ReservationDecision(
                False, False, "ledger_error", (str(exc),), ref, cid, collateral,
                0.0, deployed, available)

    def admit(self, *, throttle_recorded: bool = False, intent=None,
              **dims) -> ReservationDecision:
        """Public API contract; production-derived narrative omitted."""
        return self._admit(place=None, recheck=None, on_placed=None,
                           throttle_recorded=throttle_recorded, intent=intent, dims=dims)

    def reserve_and_place(self, *, place: Callable[[], Any],
                          recheck: Optional[Callable[[], tuple]] = None,
                          on_placed: Optional[Callable[[], bool]] = None,
                          throttle_recorded: bool = False,
                          intent=None,
                          **dims) -> ReservationDecision:
        """Public API contract; production-derived narrative omitted."""
        return self._admit(place=place, recheck=recheck, on_placed=on_placed,
                           throttle_recorded=throttle_recorded, intent=intent, dims=dims)

    def _admit(self, *, place, recheck, on_placed, throttle_recorded, dims,
               intent=None) -> ReservationDecision:
        ref = ""
        cid = 0
        collateral = 0.0
        deployed = available = 0.0
        try:
            missing = [d for d in ADMISSION_DIMENSIONS if d not in dims]
            if missing:
                raise EntryReservationError(
                    "admission refused: missing dimension(s) %s -- an unspecified ceiling is "
                    "never treated as an absent one" % ", ".join(missing))
            unknown = sorted(set(dims) - set(ADMISSION_DIMENSIONS)
                             - OPTIONAL_ADMISSION_DIMENSIONS)
            if unknown:
                raise EntryReservationError(
                    "admission refused: unknown dimension(s) %s" % ", ".join(unknown))
            n = self._normalize(dims)
            ref, cid = n["ref"], n["con_id"]
            collateral, deployed, available = n["collateral"], n["deployed"], n["available"]
            now = _finite(self.clock(), name="clock")
            if intent is None:
                raise EntryReservationError(
                    "admission refused: durable entry intent is required before transmission")


            payload = dict(intent)
            payload.setdefault("order_ref", ref)
            payload.setdefault("con_id", cid)
            payload.setdefault("leg_con_ids", n["legs"])
            payload.setdefault("symbol", n["symbol"])
            payload.setdefault("side", n["side"])
            payload.setdefault("structure", n["structure"])
            payload.setdefault("decision_id", n["envelope_id"])
            payload.setdefault("requested_qty", n["contracts"])
            payload.setdefault("estimated_debit", n["capital"])
            payload.setdefault("code_version", n["code_version"])
            payload.setdefault("policy_version", n["policy_version"])
            payload.setdefault("final_contract", n["final_contract"])
            payload.setdefault("structured_intent", n["structured_intent"])
            payload.setdefault("campaign_add_intent", n["campaign_add"])
            intent_row = _normalize_intent(payload, now=now)
            if intent_row["order_ref"] != ref:
                raise EntryReservationError(
                    "entry intent order_ref does not match the admission's order_ref")
            if intent_row["decision_id"] != n["envelope_id"]:
                raise EntryReservationError(
                    "entry intent decision_id conflicts with admission envelope_id")
            if (intent_row["code_version"] != n["code_version"]
                    or intent_row["policy_version"] != n["policy_version"]):
                raise EntryReservationError(
                    "entry intent code/policy identity conflicts with admission identity")




            conflicts = []
            if intent_row["con_id"] != cid:
                conflicts.append("con_id")
            if tuple(intent_row["leg_con_ids"]) != tuple(n["legs"]):
                conflicts.append("leg_con_ids")
            if intent_row["symbol"] != n["symbol"]:
                conflicts.append("symbol")
            if intent_row["side"] != n["side"]:
                conflicts.append("side")
            if intent_row["structure"] != n["structure"]:
                conflicts.append("structure")
            if intent_row["requested_qty"] != n["contracts"]:
                conflicts.append("requested_qty")
            if (intent_row["estimated_debit"] is None
                    or float(intent_row["estimated_debit"]) <= 0
                    or (n["side"] == "debit"
                        and abs(float(intent_row["estimated_debit"]) - n["capital"]) > 0.01)):
                conflicts.append("estimated_debit")
            if intent_row["final_contract"] != n["final_contract"]:
                conflicts.append("final_contract")
            if intent_row["structured_intent"] != n["structured_intent"]:
                conflicts.append("structured_intent")
            if _campaign_add_document(intent_row["campaign_add_intent"]) != n["campaign_add"]:
                conflicts.append("campaign_add_intent")
            if conflicts:
                raise EntryReservationError(
                    "entry intent conflicts with admission dimension(s): %s"
                    % ", ".join(conflicts))
            template = intent_row.get("journal_template")
            if not isinstance(template, dict) or not template:
                raise EntryReservationError(
                    "entry intent journal_template must be a non-empty mapping")
            try:
                template_con_id = _con_id(template.get("contract_id"))
            except EntryReservationError as exc:
                raise EntryReservationError(
                    "entry intent journal_template needs the admitted contract_id: %s" % exc
                ) from exc
            if template_con_id != cid:
                raise EntryReservationError(
                    "entry intent journal_template contract_id conflicts with admission")
            if _text(template.get("symbol"), limit=32, upper=True) != n["symbol"]:
                raise EntryReservationError(
                    "entry intent journal_template symbol conflicts with admission")
            if _text(template.get("right"), limit=1, upper=True) not in ("C", "P"):
                raise EntryReservationError(
                    "entry intent journal_template right must be C or P")
            expiry = str(template.get("expiry") or "").strip()
            if len(expiry) != 8 or not expiry.isdigit():
                raise EntryReservationError(
                    "entry intent journal_template expiry must be YYYYMMDD")
            _finite(template.get("strike"), name="journal_template.strike", positive=True)
            template_spread = template.get("spread")
            if len(n["legs"]) > 1:
                other_legs = tuple(leg for leg in n["legs"] if leg != cid)
                if len(n["legs"]) != 2 or len(other_legs) != 1:
                    raise EntryReservationError(
                        "entry intent supports exactly one primary and one short spread leg")
                if not isinstance(template_spread, dict):
                    raise EntryReservationError(
                        "multi-leg entry intent journal_template needs spread recovery facts")
                try:
                    template_short = _con_id(template_spread.get("short_con_id"))
                except EntryReservationError as exc:
                    raise EntryReservationError(
                        "spread recovery needs the admitted short_con_id: %s" % exc) from exc
                if template_short != other_legs[0]:
                    raise EntryReservationError(
                        "spread journal_template short_con_id conflicts with admitted legs")
                template_strike = _finite(
                    template.get("strike"), name="journal_template.strike", positive=True)
                template_short_strike = _finite(
                    template_spread.get("short_strike"),
                    name="journal_template.spread.short_strike", positive=True)
                template_width = _finite(
                    template_spread.get("width"),
                    name="journal_template.spread.width", positive=True)
                if abs(template_width - abs(template_short_strike - template_strike)) > 0.000001:
                    raise EntryReservationError(
                        "spread journal_template width conflicts with its strikes")
            elif template_spread not in (None, {}):
                raise EntryReservationError(
                    "single-leg entry intent may not carry spread recovery facts")
            if n["side"] == "credit":
                template_side = _text(template.get("side"), limit=8).lower()
                template_action = _text(template.get("action"), limit=8).upper()
                if template_side != "credit" or template_action != "SELL":
                    raise EntryReservationError(
                        "credit intent journal_template must identify a SELL credit position")
                template_collateral = _finite(
                    template.get("collateral_usd"),
                    name="journal_template.collateral_usd", positive=True)
                template_loss = _finite(
                    template.get("max_loss_usd"),
                    name="journal_template.max_loss_usd", positive=True)
                template_debit = _finite(
                    template.get("debit"), name="journal_template.debit", positive=True)
                template_credit = _finite(
                    template.get("net_credit_usd"),
                    name="journal_template.net_credit_usd", positive=True)
                if (abs(template_collateral - n["capital"]) > 0.01
                        or abs(template_loss - float(intent_row["estimated_debit"])) > 0.01
                        or abs(template_debit - template_loss) > 0.01
                        or abs(template_collateral - template_loss - template_credit) > 0.01):
                    raise EntryReservationError(
                        "credit intent recovery economics conflict with admission")
                final = n["final_contract"]
                if (abs(template_collateral - float(final["collateral_usd"])) > 0.01
                        or abs(template_loss - float(final["max_loss_usd"])) > 0.01
                        or abs(template_credit - float(final["net_credit_usd"])) > 0.01):
                    raise EntryReservationError(
                        "credit intent recovery economics conflict with final_contract")
            with self._locked():
                state = self._load_state()
                before = json.dumps(state, sort_keys=True)
                decision, row = self._decide_locked(state, n, now=now)
                if row is None:
                    if json.dumps(state, sort_keys=True) != before:
                        self._write_state(state)
                    return decision




                if recheck is not None:
                    ok, why = recheck()
                    if not _bool(ok, name="recheck() first element"):
                        if json.dumps(state, sort_keys=True) != before:
                            self._write_state(state)
                        return ReservationDecision(
                            False, False, "markers_set",
                            tuple(str(x) for x in (why or ("a halt marker was set after "
                                                           "admission and before transmit",))),
                            ref, cid, collateral, self._sum(state["reservations"]),
                            deployed, available, side=n["side"], symbol=n["symbol"],
                            capital_usd=n["capital"])




                row["throttle_recorded"] = bool(throttle_recorded)
                state["reservations"][ref] = row
                if intent_row is not None:



                    intent_row["created_at"] = now
                    intent_row["updated_at"] = now
                    intent_row["admission_receipt"] = row.get("admission_receipt")
                    state.setdefault("intents", {})[ref] = intent_row
                self._write_state(state)
                if place is None:
                    return decision







                from exitmgr.order_lock import OrderMutationBusy
                invoked = True
                try:
                    place()
                except BaseException as exc:
                    marked = getattr(exc, "alfred_place_invoked", None)
                    if isinstance(marked, bool):
                        invoked = marked
                    elif isinstance(exc, OrderMutationBusy):
                        invoked = False
                    if not invoked:
                        state["reservations"].pop(ref, None)




                        state.get("intents", {}).pop(ref, None)
                    elif intent_row is not None and ref in state.get("intents", {}):
                        state["intents"][ref]["transmitted"] = "unknown"
                        state["intents"][ref]["updated_at"] = _finite(
                            self.clock(), name="clock")
                    try:
                        self._write_state(state)
                    except BaseException as persistence_exc:


                        raise _PlacementFailure(persistence_exc, invoked) from None
                    raise _PlacementFailure(exc, invoked) from None

                if intent_row is not None and ref in state.get("intents", {}):
                    state["intents"][ref]["transmitted"] = "yes"
                    state["intents"][ref]["updated_at"] = _finite(self.clock(), name="clock")
                    try:
                        self._write_state(state)
                    except Exception as exc:




                        return replace(
                            decision, status="placed_persistence_pending",
                            reasons=("order placed; durable transmit-state finalization pending: "
                                     + str(exc),), place_invoked=True)
                if on_placed is not None:
                    try:
                        if on_placed():
                            state["reservations"][ref]["throttle_recorded"] = True
                            self._write_state(state)
                    except Exception:


                        pass
                return replace(decision, place_invoked=True)
        except _PlacementFailure as failure:
            try:
                setattr(failure.original, "alfred_place_invoked", failure.invoked)
            except Exception:
                pass
            raise failure.original
        except Exception as exc:
            return ReservationDecision(
                False, False, "ledger_error", (str(exc),), ref, cid, collateral,
                0.0, deployed, available)

    def reconcile(self, *, broker_deployed_usd,
                  visible_order_refs: Optional[Iterable] = None,
                  readable: bool = True) -> ReconcileResult:
        """Public API contract; production-derived narrative omitted."""
        if not _bool(readable, name="readable"):
            raise EntryReservationError(
                "refusing to reconcile against an unreadable order book: an unknown visibility "
                "feed is not an empty one")
        deployed = _finite(
            broker_deployed_usd, name="broker_deployed_usd", non_negative=True)
        refs = _visible_refs(visible_order_refs)
        now = _finite(self.clock(), name="clock")
        with self._locked():
            state = self._load_state()
            before = json.dumps(state, sort_keys=True)
            visible, expired = self._reconcile_locked(
                state, now=now, broker_deployed_usd=deployed,
                visible_order_refs=refs)
            if json.dumps(state, sort_keys=True) != before:
                self._write_state(state)
            return ReconcileResult(
                visible, expired, self._sum(state["reservations"]),
                len(state["reservations"]))

    def clear(self, order_ref) -> bool:
        """Public API contract; production-derived narrative omitted."""
        ref = _order_ref(order_ref)
        with self._locked():
            state = self._load_state()
            existed = state["reservations"].pop(ref, None) is not None
            if existed:
                self._write_state(state)
            return existed

    def clear_not_transmitted(self, order_ref) -> bool:
        """Public API contract; production-derived narrative omitted."""
        ref = _order_ref(order_ref)
        with self._locked():
            state = self._load_state()
            reservation = state["reservations"].pop(ref, None)
            intent = state.get("intents", {}).pop(ref, None)
            if reservation is not None or intent is not None:
                self._write_state(state)
            return reservation is not None or intent is not None

    def clear_for_status(self, order_ref, status) -> bool:
        """Public API contract; production-derived narrative omitted."""
        normalized = str(status or "").replace("_", "").replace(" ", "").lower()
        return self.clear(order_ref) if normalized in RELEASING_STATUSES else False



    def record_intent(self, **payload) -> dict:
        """Public API contract; production-derived narrative omitted."""
        now = _finite(self.clock(), name="clock")
        row = _normalize_intent(payload, now=now)
        row["created_at"] = row.get("created_at") or now
        row["updated_at"] = now
        with self._locked():
            state = self._load_state()
            state.setdefault("intents", {})[row["order_ref"]] = row
            self._write_state(state)
        return json.loads(json.dumps(row))

    def update_intent(self, order_ref, **updates) -> Optional[dict]:
        """Public API contract; production-derived narrative omitted."""
        ref = _order_ref(order_ref)
        now = _finite(self.clock(), name="clock")
        with self._locked():
            state = self._load_state()
            existing = state.get("intents", {}).get(ref)
            if existing is None:
                return None
            merged = dict(existing)
            merged.update(updates)
            merged["order_ref"] = ref
            merged["updated_at"] = now
            row = _normalize_intent(merged, now=now)
            state["intents"][ref] = row
            self._write_state(state)
            return json.loads(json.dumps(row))

    def resolve_intent(self, order_ref, *, outcome, filled_qty=None) -> Optional[dict]:
        """Public API contract; production-derived narrative omitted."""
        ref = _order_ref(order_ref)
        outcome = str(outcome or "")
        if outcome not in INTENT_RESOLUTIONS:
            raise EntryReservationError(
                "refusing to release entry intent %r: unknown outcome %r (permitted: %s)"
                % (ref, outcome, ", ".join(sorted(INTENT_RESOLUTIONS))))
        if outcome == "terminal_no_fill" and filled_qty:
            raise EntryReservationError(
                "refusing to release entry intent %r as terminal_no_fill: the broker reported "
                "%s filled -- that position must be journalled, not forgotten" % (ref, filled_qty))
        if outcome == "journaled" and not filled_qty:
            raise EntryReservationError(
                "refusing to release entry intent %r as journaled without a filled quantity: an "
                "active row is only ever materialised from an observed fill" % ref)
        with self._locked():
            state = self._load_state()
            row = state.get("intents", {}).pop(ref, None)
            if row is None:
                return None
            self._write_state(state)
            return json.loads(json.dumps(row))

    def journal_and_resolve_intent(self, order_ref, *, row, filled_qty,
                                   journal_append, already_journalled=None) -> dict:
        """Public API contract; production-derived narrative omitted."""
        ref = _order_ref(order_ref)
        qty = int(_finite(filled_qty, name="filled_qty", positive=True))
        if not callable(journal_append):
            raise EntryReservationError("journal_append must be callable")
        with self._locked():
            state = self._load_state()
            intent = state.get("intents", {}).get(ref)
            if intent is None:
                return {"resolved": False, "duplicate": True, "filled_qty": qty}
            duplicate = bool(intent.get("journaled"))
            if callable(already_journalled):
                try:
                    membership = already_journalled(ref)
                except Exception as exc:
                    raise EntryReservationError(
                        "entry journal membership check failed: %s" % exc) from exc
                if membership is None:
                    raise EntryReservationError("entry journal membership is unknown")
                duplicate = duplicate or bool(membership)
            if not duplicate:
                journal_append(dict(row))
            state["intents"].pop(ref, None)
            state["updated_at"] = _finite(self.clock(), name="clock")
            self._write_state(state)
            return {"resolved": True, "duplicate": duplicate, "filled_qty": qty}

    def open_intents(self) -> tuple:
        """Public API contract; production-derived narrative omitted."""
        with self._locked():
            state = self._load_state()
            rows = list(state.get("intents", {}).values())
        rows.sort(key=lambda r: (float(r.get("created_at") or 0.0), r["order_ref"]))
        return tuple(json.loads(json.dumps(r)) for r in rows)

    def note_intent_observed(self, order_ref) -> None:
        """Public API contract; production-derived narrative omitted."""
        ref = _order_ref(order_ref)
        with self._locked():
            state = self._load_state()
            row = state.get("intents", {}).get(ref)
            if row is None:
                return
            row["observations"] = int(row.get("observations") or 0) + 1
            row["updated_at"] = _finite(self.clock(), name="clock")
            self._write_state(state)

    def snapshot(self) -> dict:
        """Public API contract; production-derived narrative omitted."""
        now = _finite(self.clock(), name="clock")
        with self._locked():
            state = self._load_state()
            before = json.dumps(state, sort_keys=True)
            self._reconcile_locked(
                state, now=now, broker_deployed_usd=0.0,
                visible_order_refs=frozenset())
            if json.dumps(state, sort_keys=True) != before:
                self._write_state(state)
            return json.loads(json.dumps(state))


def reservation_as_dict(decision: ReservationDecision) -> dict:
    """Public API contract; production-derived narrative omitted."""
    return asdict(decision)


def bootstrap_production_default(*, legacy_entry_processes_stopped: bool,
                                 absent_legacy_verified_empty: bool = False) -> dict:
    """Public API contract; production-derived narrative omitted."""
    ledger = EntryReservationLedger(
        bootstrap_default=True,
        legacy_entry_processes_stopped=legacy_entry_processes_stopped,
        absent_legacy_verified_empty=absent_legacy_verified_empty)
    return {
        "ledger_path": str(ledger.ledger_path),
        "lock_path": str(ledger.lock_path),
        "legacy_fence_path": str(ledger._legacy_ledger_path),
        "legacy_source_backup_path": (
            str(ledger.legacy_backup_path) if ledger.legacy_backup_path.exists() else None),
        "migration_receipt_path": str(ledger.migration_receipt_path),
    }


def _bootstrap_cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Explicitly fence the legacy entry ledger and bootstrap durable storage")
    parser.add_argument("--legacy-entry-processes-stopped", action="store_true", required=True,
                        help="attest every old entry-capable process/job is stopped")
    parser.add_argument("--absent-legacy-verified-empty", action="store_true",
                        help="attest broker/history reconciliation proved absent legacy is empty")
    args = parser.parse_args()
    result = bootstrap_production_default(
        legacy_entry_processes_stopped=args.legacy_entry_processes_stopped,
        absent_legacy_verified_empty=args.absent_legacy_verified_empty)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_bootstrap_cli())
