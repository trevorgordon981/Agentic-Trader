"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from datetime import timedelta
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import re
from typing import Iterable, Optional, Sequence


DEFAULT_PATH = os.path.expanduser(
    os.environ.get("TRADER_WATCHLIST_PATH", "~/trade-capture/trader_watchlist.md"))
DEFAULT_WAIT_FOR = (
    "Wait for a pullback, red/inside session, or other volatility reset; then reassess from "
    "fresh market and option data.  A red day alone is not an automatic entry."
)
MAX_BYTES = 32 * 1024


MAX_TARGETS = 35
DEFAULT_TTL_DAYS = 30
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_HEADING_RE = re.compile(r"^##\s+([A-Za-z][A-Za-z0-9.\-]{0,9})\s*$")
_FIELD_RE = re.compile(r"^-\s+([A-Za-z][A-Za-z _-]*):\s*(.*)$")


@dataclass(frozen=True)
class SetupTarget:
    symbol: str
    status: str = "active"
    bias: str = "fresh-evidence-decides"
    added: str = ""
    setup: str = ""
    wait_for: str = DEFAULT_WAIT_FOR
    source: str = ""
    expires: str = ""


@dataclass(frozen=True)
class WatchlistSnapshot:
    targets: tuple[SetupTarget, ...] = ()
    warnings: tuple[str, ...] = ()
    sha256: Optional[str] = None

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(t.symbol for t in self.targets)


def _normal_symbol(value: object) -> Optional[str]:
    symbol = str(value or "").strip().upper()
    return symbol if _SYMBOL_RE.fullmatch(symbol) else None


def _clean_field(value: object, limit: int, default: str = "") -> str:
    """Public API contract; production-derived narrative omitted."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or default))
    cleaned = cleaned.replace("#", "")
    return re.sub(r"\s+", " ", cleaned).strip()[:limit]


def read_watchlist(path: str = DEFAULT_PATH, *, today: Optional[date] = None) -> WatchlistSnapshot:
    """Public API contract; production-derived narrative omitted."""
    p = Path(os.path.expanduser(path))
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return WatchlistSnapshot()
    except OSError as exc:
        return WatchlistSnapshot(warnings=(f"watchlist unreadable: {exc}",))
    digest = sha256(raw).hexdigest()
    if len(raw) > MAX_BYTES:
        return WatchlistSnapshot(
            warnings=(f"watchlist exceeds {MAX_BYTES} bytes; ignored",), sha256=digest)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return WatchlistSnapshot(warnings=(f"watchlist is not UTF-8: {exc}",), sha256=digest)

    today = today or datetime.now(timezone.utc).date()
    warnings: list[str] = []
    sections: list[tuple[str, dict[str, str]]] = []
    current_symbol: Optional[str] = None
    fields: dict[str, str] = {}

    def finish() -> None:
        nonlocal current_symbol, fields
        if current_symbol:
            sections.append((current_symbol, fields))
        current_symbol, fields = None, {}

    for lineno, line in enumerate(text.splitlines(), 1):
        heading = _HEADING_RE.match(line.strip())
        if heading:
            finish()
            current_symbol = _normal_symbol(heading.group(1))
            if current_symbol is None:
                warnings.append(f"invalid target heading on line {lineno}")
            continue
        if current_symbol:
            field = _FIELD_RE.match(line.strip())
            if field:
                fields[field.group(1).strip().lower().replace(" ", "_")] = field.group(2).strip()
    finish()

    out: list[SetupTarget] = []
    seen: set[str] = set()
    for symbol, item in sections:
        status = item.get("status", "active").strip().lower()
        if status not in {"active", "watching"}:
            continue
        if symbol in seen:
            warnings.append(f"duplicate active target {symbol}; first active entry kept")
            continue
        seen.add(symbol)
        expires = item.get("expires", "").strip()
        if expires:
            try:
                if date.fromisoformat(expires) < today:
                    continue
            except ValueError:
                warnings.append(f"invalid expiry for {symbol}; kept active")
        out.append(SetupTarget(
            symbol=symbol,
            status=status,
            bias=item.get("bias", "fresh-evidence-decides")[:80],
            added=item.get("added", "")[:32],
            setup=item.get("setup", "")[:500],
            wait_for=item.get("wait_for", item.get("wait-for", DEFAULT_WAIT_FOR))[:500],
            source=item.get("source", "")[:500],
            expires=expires[:32],
        ))
        if len(out) >= MAX_TARGETS:
            warnings.append(f"watchlist capped at {MAX_TARGETS} active targets")
            break
    return WatchlistSnapshot(tuple(out), tuple(warnings), digest)


def render_for_brief(snapshot: WatchlistSnapshot) -> str:
    """Public API contract; production-derived narrative omitted."""
    if not snapshot.targets:
        return ""
    lines = [
        "Persistent setup watchlist (context only; never an order or approval):",
        "  Re-underwrite every target from the fresh quote, price structure, events, and option "
        "data in this brief. Recorded notes may be stale. A pullback/red day is permission to "
        "reassess, not an automatic buy; waiting or rejecting the setup is valid.",
    ]
    for target in snapshot.targets:
        bits = [f"bias={target.bias or 'fresh-evidence-decides'}"]
        if target.setup:
            bits.append(f"setup={target.setup}")
        bits.append(f"wait_for={target.wait_for or DEFAULT_WAIT_FOR}")
        lines.append(f"  {target.symbol}: " + " | ".join(bits))
    return "\n".join(lines)


def prioritize_for_opening(core: Sequence[str], rotation: Sequence[str],
                           targets: Iterable[str], *, non_core_limit: int = 35) -> list[str]:
    """Public API contract; production-derived narrative omitted."""
    core_out: list[str] = []
    seen: set[str] = set()
    for raw in core:
        symbol = _normal_symbol(raw)
        if symbol and symbol not in seen:
            seen.add(symbol)
            core_out.append(symbol)
    focus: list[str] = []
    for raw in targets:
        symbol = _normal_symbol(raw)
        if symbol and symbol not in seen:
            seen.add(symbol)
            focus.append(symbol)
    rest: list[str] = []
    for raw in rotation:
        symbol = _normal_symbol(raw)
        if symbol and symbol not in seen:
            seen.add(symbol)
            rest.append(symbol)
    non_core_limit = max(0, int(non_core_limit))
    return core_out + (focus + rest)[:non_core_limit]


def _document(targets: Sequence[SetupTarget]) -> str:
    lines = [
        "# Trader Setup Watchlist",
        "",
        "Persistent entry-timing context for the opening slate. This file is not trading,",
        "contract, approval, sizing, or execution authority.",
        "",
    ]
    for target in targets:
        lines.extend([
            f"## {target.symbol}",
            f"- Status: {target.status or 'active'}",
            f"- Bias: {target.bias or 'fresh-evidence-decides'}",
            f"- Added: {target.added or datetime.now(timezone.utc).date().isoformat()}",
            f"- Setup: {target.setup or 'Reassess from fresh evidence.'}",
            f"- Wait for: {target.wait_for or DEFAULT_WAIT_FOR}",
        ])
        if target.source:
            lines.append(f"- Source: {target.source}")
        if target.expires:
            lines.append(f"- Expires: {target.expires}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _archive_symbol_sections(text: str, symbol: str) -> str:
    """Public API contract; production-derived narrative omitted."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if _HEADING_RE.match(line.strip())]
    starts.append(len(lines))
    for pos in range(len(starts) - 1):
        start, end = starts[pos], starts[pos + 1]
        heading = _HEADING_RE.match(lines[start].strip())
        if not heading or _normal_symbol(heading.group(1)) != symbol:
            continue
        status_line = None
        for i in range(start + 1, end):
            field = _FIELD_RE.match(lines[i].strip())
            if field and field.group(1).strip().lower().replace(" ", "_") == "status":
                status_line = i
                break
        if status_line is None:
            lines.insert(start + 1, "- Status: archived")

            return _archive_symbol_sections("\n".join(lines) + "\n", symbol)
        if lines[status_line].split(":", 1)[-1].strip().lower() in {"active", "watching"}:
            lines[status_line] = "- Status: archived"
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def add_target(symbol: str, *, setup: str, bias: str = "fresh-evidence-decides",
               wait_for: str = DEFAULT_WAIT_FOR, source: str = "",
               path: str = DEFAULT_PATH, added: Optional[str] = None,
               expires: Optional[str] = None) -> bool:
    """Public API contract; production-derived narrative omitted."""
    normal = _normal_symbol(symbol)
    if normal is None:
        raise ValueError(f"invalid ticker: {symbol!r}")
    p = Path(os.path.expanduser(path))
    today = datetime.now(timezone.utc).date()
    expiry = expires or (today + timedelta(days=DEFAULT_TTL_DAYS)).isoformat()
    try:
        date.fromisoformat(expiry)
    except ValueError as exc:
        raise ValueError(f"invalid expiry: {expiry!r}") from exc
    target = SetupTarget(
        symbol=normal,
        status="active",
        bias=_clean_field(bias, 80, "fresh-evidence-decides"),
        added=_clean_field(added or today.isoformat(), 32),
        setup=_clean_field(setup, 500, "Reassess from fresh evidence."),
        wait_for=_clean_field(wait_for, 500, DEFAULT_WAIT_FOR),
        source=_clean_field(source, 500),
        expires=expiry,
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_name(p.name + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        snapshot = read_watchlist(str(p), today=today)
        refreshing = normal in set(snapshot.symbols)
        if not refreshing and len(snapshot.targets) >= MAX_TARGETS:
            raise ValueError(f"watchlist already has the maximum {MAX_TARGETS} active targets")
        try:
            existing = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        if refreshing:
            existing = _archive_symbol_sections(existing, normal)
        if existing.strip():


            section = _document((target,)).split("## ", 1)[1]
            updated = existing.rstrip() + "\n\n## " + section
        else:
            updated = _document((target,))
        if len(updated.encode("utf-8")) > MAX_BYTES:
            raise ValueError(f"watchlist would exceed {MAX_BYTES} bytes")
        tmp = p.with_name(p.name + f".tmp.{os.getpid()}")
        tmp.write_text(updated, encoding="utf-8")
        os.replace(tmp, p)
    return True
