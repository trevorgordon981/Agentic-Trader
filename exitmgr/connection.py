"""IB connection and market data management using ib_async."""

import asyncio
import os
from typing import Optional, Dict, List, Any
from dataclasses import dataclass

from exitmgr.ibkr import IB, Contract, Position, Order


@dataclass
class PositionData:
    """Normalized position data from IB."""
    con_id: int
    symbol: str
    right: str  # 'C' or 'P'
    quantity: int  # contracts; POSITIVE = long, NEGATIVE = short (sign is never stripped)
    avg_cost: float  # average cost per share
    expiry: str = ""  # option expiry YYYYMMDD
    # SHORT VISIBILITY (2026-07-26): two ADDITIVE fields, both defaulted so every existing
    # construction (production and tests, positional or keyword) is unchanged.
    #   sec_type -- 'OPT'/'FOP' for an option, 'STK' for assigned shares. Needed because an
    #     ASSIGNED put leaves a STOCK row that can still carry residual right/strike fields
    #     (IBKR does this), so `right` alone cannot tell an option from stock.
    #   strike   -- the option strike; a short put's collateral is strike*100*|qty|, which is
    #     the only honest way to value it (avg_cost is the premium, ~1/300th of the risk).
    sec_type: str = "OPT"
    strike: float = 0.0

    @property
    def is_short(self) -> bool:
        """True for a SOLD position. Callers that only understand long positions must test
        this (or `quantity > 0`) rather than assuming the sign."""
        return self.quantity < 0


@dataclass
class OrderData:
    """Normalized order data from IB."""
    con_id: int
    order_id: int
    remaining: int  # remaining quantity
    limit_price: Optional[float] = None
    perm_id: int = 0
    client_id: Optional[int] = None
    order_ref: Optional[str] = None
    status: Optional[str] = None


class IBConnection:
    """Wrapper around ib_async.IB for connection and data retrieval."""

    def __init__(self, host: str, port: int, client_id: int, market_data_type: int = 3):
        self.host = host
        self.port = port
        self.client_id = client_id
        # Pool is derived from the ORIGINAL id so each loop rotates inside its own
        # disjoint range and can never land on another job's clientId.
        self._base_client_id = int(client_id) if str(client_id).isdigit() else 0
        self._rotation_idx = 0
        # 1=live (needs paid subscription), 3=delayed (free; ~15min lag). Default delayed so an
        # unsubscribed account gets usable quotes instead of Error 10089 + NaN tickers.
        self.market_data_type = market_data_type
        self.ib: Optional[IB] = None
        self._connected = False
        # SELF-HEAL: tracks the gateway<->IBKR UPLINK, which a local isConnected() can NOT see.
        # On Error 1100 the uplink drops but our 127.0.0.1 TCP socket stays open, so
        # isConnected() lies True. The errorEvent handler flips this so is_healthy() is honest.
        self._uplink_ok = True
        # Set when a broker call is refused; forces the next health check to fail so the
        # loop repairs the link instead of trusting a stale 'healthy' verdict.
        self._link_fault = False

    async def connect(self, retries: int = 0, retry_delay: float = 30.0, force: bool = False) -> bool:
        """Establish connection to IB Gateway/TWS.

        retries: extra attempts after the first if connect fails (0 = original single-shot
        behavior, used by the trader loop which self-heals each cycle). Once-daily jobs pass
        retries>0 to ride out a brief gateway auto-restart window. NOTE: a gateway sitting
        LOGGED OUT awaiting periodic 2FA will fail every attempt -- retry can't bypass 2FA."""
        # force=True tears down a (possibly stale) link first so a reconnect is never
        # short-circuited by the early-return below (the Error-1100 stale-socket case).
        if force:
            self._connected = False
            try:
                if self.ib:
                    self.ib.disconnect()
            except Exception:
                pass
            self.ib = None
            # STALE-SESSION GUARD (2026-06-29): after a 1100/peer-close the gateway keeps the
            # old clientId reserved for several seconds, so reconnecting with the SAME id ->
            # Error 326 "client id already in use" and the reconnect (and any pending exit
            # order) fails every retry. Account/positions are clientId-independent, so rotate
            # to a fresh high id that won't collide with the other jobs' ids.
            self.client_id = self._next_rotation_id()
            n = self._record_rotation()
            print(f"[INFO] reconnect: rotating to fresh clientId={self.client_id} "
                  f"(avoids Error 326; rotation {n} today)")
        if self._connected:
            return True

        attempt = 0
        while True:
            self.ib = IB()
            try:
                # ib_async already applies timeout=10 below; this outer bound is a BACKSTOP
                # (2026-08-13) for the case where the library timeout does not fire -- the one
                # failure mode here is "the reconnect loop never returns".
                _CONNECT_BACKSTOP_S = 30
                await asyncio.wait_for(self.ib.connectAsync(
                    host=self.host,
                    port=self.port,
                    clientId=self.client_id,
                    timeout=10,
                ), _CONNECT_BACKSTOP_S)
                self._connected = True
                self._uplink_ok = True
                self._link_fault = False
                # (re)subscribe the uplink-health handler on every fresh IB() instance.
                try:
                    self.ib.errorEvent += self._on_error
                except Exception as _e:
                    print(f"[WARN] could not subscribe errorEvent: {_e}")
                self.ib.reqMarketDataType(self.market_data_type)
                print(f"[INFO] Connected to IB at {self.host}:{self.port} "
                      f"(client_id={self.client_id}, market_data_type={self.market_data_type})")
                return True
            except Exception as e:
                print(f"[ERROR] Failed to connect to IB (attempt {attempt+1}/{retries+1}): {str(e) or type(e).__name__}")
                self._connected = False
                try:
                    if self.ib:
                        self.ib.disconnect()
                except Exception:
                    pass
                self.ib = None
                if attempt >= retries:
                    return False
                attempt += 1
                print(f"[INFO] retrying IB connect in {retry_delay:.0f}s ...")
                await asyncio.sleep(retry_delay)

    async def disconnect(self) -> None:
        """Disconnect from IB."""
        if self.ib and self._connected:
            try:
                self.ib.disconnect()
                print("[INFO] Disconnected from IB")
            except Exception as e:
                print(f"[WARN] Error during disconnect: {e}")
            finally:
                self._connected = False
                self.ib = None

    def _on_error(self, reqId, errorCode, errorString, contract=None):
        """ib_async errorEvent handler. Tracks the gateway<->IBKR UPLINK so is_healthy()
        can tell a stale link from a live one (isConnected() can't). MUST NOT raise.
        1100 = connectivity LOST; 1300 = socket dropped/reset; 2110 = connectivity restored
        but data farm broken -> treat as not-ok. 1102 = RESTORED (data kept) -> ok.
        2104/2106/2158/2119 = market-data / HMDS / sec-def farm OK -> ALSO restore: IBKR
        answers a 2110 with a farm-OK code, never with 1102, which is why the flag was
        cleared 232x lifetime and restored only 52x.
        1101 = RESTORED but data subscriptions dropped -> ALSO ok here (quotes are
        snapshot-fetched per cycle); leaving it unhandled stuck the flag False forever."""
        try:
            if errorCode in (1100, 1300, 2110):
                self._uplink_ok = False
                print(f"[WARN] IBKR uplink DOWN (code {errorCode}): {errorString}")
            # 2119 ('farm is CONNECTING') is transitional, NOT up -- excluded on
            # purpose; 2104 follows within seconds when the farm truly comes up.
            elif errorCode in (1102, 2104, 2106, 2158):
                self._uplink_ok = True
                print(f"[INFO] IBKR uplink RESTORED (code {errorCode}): {errorString}")
            elif errorCode == 1101:
                # RESTORED, but IBKR dropped our market-data SUBSCRIPTIONS (2026-08-13). Before
                # this branch existed, a 1101 left _uplink_ok stuck False forever -- is_healthy()
                # then failed every cycle and forced a reconnect, which is what produced 115
                # clientId rotations in one day and put SMCI's trailing stop into a
                # "Not connected to IB" window.
                # Safe to treat as restored HERE because quotes are snapshot-fetched per cycle via
                # reqTickersAsync; no streaming subscription survives a cycle to be lost. If
                # streaming is ever added, this branch must also re-subscribe.
                self._uplink_ok = True
                print(f"[INFO] IBKR uplink RESTORED, data subs dropped (code {errorCode}): "
                      f"{errorString} -- quotes are re-requested per cycle, continuing")
        except Exception:
            pass

    _ROTATION_POOL_SIZE = 4
    #: Resolved at CALL time from EXITMGR_LINK_ROTATIONS_PATH so tests cannot write the live
    #: rotation counters. Same no-override class as _MGMT_ALERTED_PATH (fixed 2026-08-16).
    _ROTATION_COUNT_ENV = "EXITMGR_LINK_ROTATIONS_PATH"
    _ROTATION_COUNT_DEFAULT = os.path.expanduser("~/.local/var/exitmgr/link-rotations.json")

    @property
    def _ROTATION_COUNT_PATH(self) -> str:
        return os.environ.get(self._ROTATION_COUNT_ENV) or self._ROTATION_COUNT_DEFAULT

    def _rotation_pool(self):
        """Four ids reserved to THIS connection, disjoint from every other job's.

        base*10 + 4000 keeps the entry loop (1 -> 4010-4013) and the protective loop
        (189 -> 5890-5893) apart, and clear of daily_recommend (93), exec_capture (170),
        close_symbol (95) and the link watchdog (9731)."""
        start = 4000 + (self._base_client_id * 10)
        if start < 1000 or start > 8990:          # pathological base -> fold back into range
            start = 4000 + (abs(hash(self._base_client_id)) % 4000)
        return [start + i for i in range(self._ROTATION_POOL_SIZE)]

    def _next_rotation_id(self) -> int:
        pool = self._rotation_pool()
        cid = pool[self._rotation_idx % len(pool)]
        self._rotation_idx += 1
        return cid

    def _record_rotation(self) -> int:
        """Count rotations per day. 122 in one day (2026-08-14) meant the link was failing its
        health check constantly; single digits is normal. Persisted so the watchdog can publish
        it and alert on a rotation storm BEFORE an exit fails to transmit. Best-effort."""
        try:
            from datetime import date
            import json as _json
            today = str(date.today())
            data = {}
            try:
                with open(self._ROTATION_COUNT_PATH) as fh:
                    data = _json.load(fh)
            except Exception:
                data = {}
            if data.get("date") != today:
                data = {"date": today, "count": 0}
            data["count"] = int(data.get("count", 0)) + 1
            os.makedirs(os.path.dirname(self._ROTATION_COUNT_PATH), exist_ok=True)
            tmp = self._ROTATION_COUNT_PATH + ".tmp"
            with open(tmp, "w") as fh:
                _json.dump(data, fh)
            os.replace(tmp, self._ROTATION_COUNT_PATH)
            return data["count"]
        except Exception:
            return -1

    def is_healthy(self) -> bool:
        """Cheap, non-blocking liveness: live socket AND uplink reported up.

        MUST agree with _require_link(). On 2026-08-14 it did not: this tested
        ib.isConnected() while every broker call tested self._connected, so when the two
        diverged the loop saw a HEALTHY link and never reconnected while every call raised.
        3.5h wedge through the open, 436 failures, three stop orders lost. Do not let these
        two predicates drift apart again."""
        return bool(self.ib and self.ib.isConnected()
                    and self._uplink_ok and not self._link_fault)

    def _require_link(self):
        """Single gate for every broker call. Raising here also ARMS a reconnect: a call that
        was refused is itself evidence the link is bad, so the next ensure_connected() must
        fail and repair rather than reporting healthy."""
        if not self._connected or not self.ib or not self.ib.isConnected():
            self._link_fault = True       # cleared only by a successful connect()
            raise RuntimeError("Not connected to IB")

    async def ensure_connected(self, probe: bool = True, timeout: float = 5.0) -> bool:
        """Active liveness check. Returns True only if the link is genuinely usable.
        Beyond is_healthy() it (optionally) round-trips reqCurrentTimeAsync under a short
        timeout -- a stale link that still reports isConnected() True will hang/raise here
        and be treated as unhealthy."""
        if not self.is_healthy():
            return False
        if not probe:
            return True
        try:
            await asyncio.wait_for(self.ib.reqCurrentTimeAsync(), timeout=timeout)
            return True
        except Exception as e:
            print(f"[WARN] liveness probe failed (treating link as DOWN): {e}")
            self._uplink_ok = False
            return False

    async def reconnect(self, retries: int = 3, retry_delay: float = 10.0) -> bool:
        """Force-reconnect: tear down the (possibly stale) link and re-establish, re-subscribing
        the error handler. Returns True on success."""
        print("[WARN] forcing IBKR reconnect ...")
        return await self.connect(retries=retries, retry_delay=retry_delay, force=True)

    @staticmethod
    def _str_field(obj, name: str) -> str:
        """Read a STRING attribute defensively. Returns '' when the attribute is missing or is
        not a real string (a MagicMock in a test, a None from a partial IBKR contract). Used so
        an unreadable secType can never be mistaken for a real one."""
        v = getattr(obj, name, None)
        return v if isinstance(v, str) else ""

    @staticmethod
    def _num_field(obj, name: str) -> float:
        """Read a NUMERIC attribute defensively; 0.0 when missing/unreadable/NaN."""
        v = getattr(obj, name, None)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return 0.0
        return float(v) if v == v else 0.0

    async def get_positions(self, include_short: bool = False,
                            include_stock: bool = False) -> Dict[int, PositionData]:
        """
        Fetch all current positions using reqPositionsAsync.
        Returns dict mapping con_id to PositionData.

        DEFAULT (include_short=False, include_stock=False) -- UNCHANGED, and deliberately so:
        only LONG option positions (calls AND puts, quantity > 0). Short legs of debit spreads
        are excluded -- they are managed as a unit via the journaled long leg (see manager.py
        spread handling). EVERY pre-existing caller uses this default and sees exactly what it
        always saw. That is not laziness: an audit of the consumers (2026-07-26) found that
        feeding negatives into them silently corrupts risk gates rather than merely widening
        them -- construction.open_book_items() would add a short put's MAX LOSS to the
        long-premium deployment book (blowing the 40% deployed + 4%/day theta caps and locking
        out every entry), risk.evaluate_trade()'s single-name aggregate would do the same, and
        order.py's close path hardcodes SELL and would size an order off a negative quantity.
        Until those files are made short-aware, a caller must ASK for shorts.

        include_short=True -- ALSO returns SHORT options with the sign PRESERVED
        (quantity < 0). It is never abs()'d: a short that looks like a long to a consumer that
        does not check is a worse bug than a short that is invisible. Callers must branch on
        `quantity < 0` / `PositionData.is_short`.

        include_stock=True -- ALSO returns STOCK rows (sec_type='STK', right=''). Used solely to
        recognise an ASSIGNED cash-secured put: the option row disappears and a stock row
        appears, and without this the assignment is indistinguishable from a manual buy-back.
        Stock is NEVER fed to the option management path (its close is not an option order).
        """
        self._require_link()

        # BOUNDED (2026-08-12). This await had NO timeout. After an IBKR 1100/1102 uplink
        # flap the broker never sent the terminating event, so this coroutine awaited
        # forever -- hanging the protective loop for 84 minutes with three open positions
        # and NO stop evaluation running. launchd KeepAlive cannot help: the process stays
        # alive, parked in kevent. A bounded wait degrades that to "this cycle failed",
        # which the caller already handles and the next 30s cycle retries.
        positions = await asyncio.wait_for(self.ib.reqPositionsAsync(), 30)

        result: Dict[int, PositionData] = {}
        for pos in positions:
            # pos.contract should have conId, symbol, right
            # pos.position is the number of contracts (positive=long, negative=short)
            # pos.avgCost is average cost per share
            contract = pos.contract
            position_qty = pos.position

            # Filter: long options only (calls and puts, quantity > 0)
            # NOTE: this predicate is byte-identical to the pre-2026-07-26 one and must stay so.
            if (hasattr(contract, 'right') and contract.right in ('C', 'P') and position_qty > 0):
                con_id = contract.conId
                result[con_id] = PositionData(
                    con_id=con_id,
                    symbol=contract.symbol if hasattr(contract, 'symbol') else "",
                    right=contract.right,
                    quantity=position_qty,
                    avg_cost=pos.avgCost if hasattr(pos, 'avgCost') else 0.0,
                    expiry=getattr(contract, 'lastTradeDateOrContractMonth', '') or '',
                    sec_type=(self._str_field(contract, 'secType').upper() or "OPT"),
                    strike=self._num_field(contract, 'strike'),
                )
                continue

            if include_short and position_qty < 0:
                # A SHORT OPTION. secType gates it, not the option-shaped fields: an assigned
                # (or called-away) STOCK row can still carry a residual right/strike, and
                # closing that as an option would be a wrong-instrument order. Unreadable
                # secType ('' -- absent, None, or a test double) is treated as an option so a
                # real short can never be silently dropped by a broker field we failed to read.
                sec_type = self._str_field(contract, 'secType').upper()
                if sec_type not in ('', 'OPT', 'FOP'):
                    continue
                if not (hasattr(contract, 'right') and contract.right in ('C', 'P')):
                    continue
                con_id = contract.conId
                result[con_id] = PositionData(
                    con_id=con_id,
                    symbol=contract.symbol if hasattr(contract, 'symbol') else "",
                    right=contract.right,
                    quantity=position_qty,          # NEGATIVE -- sign preserved on purpose
                    avg_cost=pos.avgCost if hasattr(pos, 'avgCost') else 0.0,
                    expiry=getattr(contract, 'lastTradeDateOrContractMonth', '') or '',
                    sec_type=(sec_type or "OPT"),
                    strike=self._num_field(contract, 'strike'),
                )
                continue

            if include_stock and position_qty != 0:
                if self._str_field(contract, 'secType').upper() != 'STK':
                    continue
                con_id = contract.conId
                result[con_id] = PositionData(
                    con_id=con_id,
                    symbol=contract.symbol if hasattr(contract, 'symbol') else "",
                    right="",                       # stock has no right, whatever IBKR echoes
                    quantity=position_qty,          # may be negative (called-away short stock)
                    avg_cost=pos.avgCost if hasattr(pos, 'avgCost') else 0.0,
                    expiry="",
                    sec_type="STK",
                    strike=0.0,
                )

        return result

    async def get_open_orders(self, short_leg_con_ids=None) -> Dict[int, OrderData]:
        """
        Fetch all resting SELL-to-close orders, keyed by the con_id the idempotency/reconcile
        layer keys on. Returns dict mapping con_id -> OrderData.

        FIX (2026-07-03): reqOpenOrdersAsync / reqAllOpenOrdersAsync return **Trade** objects, not
        Orders. The old code read ``order.action`` / ``order.filled`` off a Trade -- ``.action``
        doesn't exist on a Trade (it's ``t.order.action``) and ``.filled`` is a *method* on a Trade,
        so ``total - filled`` raised a TypeError EVERY call and the idempotency/reconcile view was
        blind to real resting closes (a root of the double-close family). Now:
          * uses ``reqAllOpenOrdersAsync()`` so orders from EVERY clientId are seen (the close/
            liquidate tools use clientId 91; the trader loop rotates ids) -- an untracked resting
            close on another client is what jammed reconciliation;
          * iterates Trade objects: action via ``t.order.action``, remaining via
            ``t.orderStatus.remaining`` (falls back to totalQuantity-filled);
          * keys a SPREAD combo (BAG) close by the LONG-leg conId (the BUY leg of the combo), NOT
            the BAG's own conId (0/unusable) -- so the combo close matches the journaled long leg
            the idempotency check keys on.
        """
        self._require_link()

        # BOUNDED (2026-08-12). This await had NO timeout. After an IBKR 1100/1102 uplink
        # flap the broker never sent the terminating event, so this coroutine awaited
        # forever -- hanging the protective loop for 84 minutes with three open positions
        # and NO stop evaluation running. launchd KeepAlive cannot help: the process stays
        # alive, parked in kevent. A bounded wait degrades that to "this cycle failed",
        # which the caller already handles and the next 30s cycle retries.
        trades = await asyncio.wait_for(self.ib.reqAllOpenOrdersAsync(), 30)
        _short_legs = {int(c) for c in (short_leg_con_ids or [])}

        result: Dict[int, OrderData] = {}
        for t in trades or []:
            order = getattr(t, "order", None)
            contract = getattr(t, "contract", None)
            status = getattr(t, "orderStatus", None)
            if order is None or contract is None:
                continue

            # Key by the LONG-leg conId for a spread combo (BAG); else the contract's own conId.
            con_id = self._order_key_con_id(contract)
            if con_id is None:
                continue
            _action = getattr(order, "action", "")
            # Include SELL-to-close orders (the exit side we reconcile against). M1 (2026-07-09):
            # ALSO include a BUY that COVERS a KNOWN short leg (buy-to-close on a naked/over-covered
            # short) -- otherwise a resting cover is invisible to the idempotency/reconcile view and
            # could be double-placed. Any OTHER BUY (a fresh long entry) is not a close -> excluded.
            if _action != "SELL":
                if not (_action == "BUY" and con_id in _short_legs):
                    continue

            # remaining: prefer the live orderStatus, else totalQuantity - filled
            remaining = getattr(status, "remaining", None) if status is not None else None
            if not (isinstance(remaining, (int, float)) and remaining == remaining):
                total = getattr(order, "totalQuantity", 0) or 0
                filled = getattr(status, "filled", 0) if status is not None else 0
                if not isinstance(filled, (int, float)) or filled != filled:
                    filled = 0
                remaining = max(0, total - filled)

            result[con_id] = OrderData(
                con_id=con_id,
                order_id=getattr(order, "orderId", 0) or 0,
                remaining=int(remaining),
                limit_price=getattr(order, "lmtPrice", None),
                perm_id=getattr(order, "permId", 0) or 0,
                client_id=(getattr(order, "clientId", None)
                           if isinstance(getattr(order, "clientId", None), int) else None),
                order_ref=getattr(order, "orderRef", None) or None,
                status=getattr(status, "status", None) if status is not None else None,
            )

        return result

    @staticmethod
    def _order_key_con_id(contract) -> Optional[int]:
        """The con_id an order should be keyed by for idempotency/reconcile. For a single-leg
        option it is the contract's own conId. For a spread BAG combo the BAG has no usable conId,
        so key by the LONG leg -- the BUY leg of the combo (a debit close SELLs the BUY/SELL combo
        that was bought, so the BUY leg is the long leg the journal + idempotency check key on)."""
        if getattr(contract, "secType", "") == "BAG":
            legs = getattr(contract, "comboLegs", None) or []
            for leg in legs:
                if getattr(leg, "action", "") == "BUY" and getattr(leg, "conId", None):
                    return int(leg.conId)
            # no explicit BUY leg found -> fall back to the first leg's conId
            for leg in legs:
                if getattr(leg, "conId", None):
                    return int(leg.conId)
            return None
        cid = getattr(contract, "conId", None)
        return int(cid) if cid else None

    async def fetch_quotes(self, con_ids: List[int]) -> Dict[int, dict]:
        """
        Fetch market quotes for given contract IDs using reqTickersAsync.
        Returns dict mapping con_id to quote data (bid, ask, last, mark).
        Skips contracts with NaN/stale data.
        """
        self._require_link()

        if not con_ids:
            return {}

        # Create contracts for each con_id
        contracts = []
        con_id_to_idx: Dict[int, int] = {}
        for i, con_id in enumerate(con_ids):
            contract = Contract()
            contract.conId = con_id
            contracts.append(contract)
            con_id_to_idx[con_id] = i

        # Qualify the conId-only contracts FIRST -- reqTickers on unqualified contracts hangs/times
        # out; qualifying makes option quotes actually stream (restores mechanical TP/SL evaluation).
        # BOUNDED (2026-08-13). Both reads below were unbounded. The exit engine calls this every
        # cycle, so a stalled broker read here leaves EVERY position unevaluated -- observed live
        # tonight as a 6-minute wedge with read/kevent at the stack leaves. A timeout degrades to
        # "no quotes this cycle" (an existing, tested path that simply skips and retries in 30s),
        # which is strictly better than never returning.
        _QUOTE_FETCH_TIMEOUT_S = 30
        try:
            qc = await asyncio.wait_for(
                self.ib.qualifyContractsAsync(*contracts), _QUOTE_FETCH_TIMEOUT_S)
            contracts = [c for c in qc if getattr(c, "conId", None)] or contracts
        except asyncio.TimeoutError:
            print(f"[WARN] qualify timed out after {_QUOTE_FETCH_TIMEOUT_S}s in fetch_quotes; "
                  f"continuing with unqualified contracts")
        except Exception as e:
            print(f"[WARN] qualify failed in fetch_quotes: {e}")
        # Request tickers
        try:
            tickers = await asyncio.wait_for(
                self.ib.reqTickersAsync(*contracts), _QUOTE_FETCH_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Do NOT hang the exit engine. No quotes -> the manager's existing
            # "No valid quote ... skipping" path runs, the cycle finishes, and we retry next cycle.
            print(f"[ERROR] reqTickers timed out after {_QUOTE_FETCH_TIMEOUT_S}s for "
                  f"{len(contracts)} contracts; returning no quotes this cycle (stops re-evaluate "
                  f"on the next cycle)")
            return {}
        except Exception as e:
            print(f"[ERROR] reqTickers failed in fetch_quotes ({e}); returning no quotes this cycle")
            return {}

        result: Dict[int, dict] = {}
        for ticker in tickers:
            if ticker.contract and hasattr(ticker.contract, 'conId'):
                con_id = ticker.contract.conId

                # Extract prices, check for NaN/stale
                bid = ticker.bid if hasattr(ticker, 'bid') else None
                ask = ticker.ask if hasattr(ticker, 'ask') else None
                last = ticker.last if hasattr(ticker, 'last') else None
                mark = ticker.mark if hasattr(ticker, 'mark') else None

                # Use mark if available and valid, else midpoint
                if mark is not None and not (mark != mark):  # NaN check
                    price = mark
                elif bid is not None and ask is not None and bid > 0 and ask > 0 and not (bid != bid) and not (ask != ask):
                    price = (bid + ask) / 2.0
                elif last is not None and not (last != last) and last > 0:
                    price = last
                else:
                    # Invalid/stale data - skip this contract
                    print(f"[WARN] Skipping con_id={con_id} due to stale/NaN quote data")
                    continue

                # Greeks/IV ride along when the ticker already carries them (no extra request)
                # -- logging enrichment for the 2026-07-01 audit trail; None when unavailable.
                g = getattr(ticker, "modelGreeks", None) or getattr(ticker, "lastGreeks", None)
                iv = getattr(g, "impliedVol", None) if g else None
                delta = getattr(g, "delta", None) if g else None
                result[con_id] = {
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "mark": mark,
                    "price": price,  # normalized price per share
                    "iv": iv if (iv is not None and iv == iv) else None,
                    "delta": delta if (delta is not None and delta == delta) else None,
                }

        return result

    async def place_order(
        self,
        contract: Contract,
        order: Order,
    ) -> Order:
        """
        Place an order using ib's async order placement.
        Returns the order with orderId filled in.
        """
        self._require_link()

        # ib_async: placeOrder is SYNCHRONOUS and returns a Trade immediately
        from exitmgr.order_lock import order_mutation_lock
        with order_mutation_lock():
            trade = self.ib.placeOrder(contract, order)
        return trade

    def reserve_order_id(self) -> int:
        """Reserve this client's next IB order id before transmission.

        ib_async normally performs the same allocation inside ``placeOrder``.  Exposing it one
        step earlier lets the exit manager fsync the exact client-scoped identity first.
        """
        self._require_link()
        client = getattr(self.ib, "client", None)
        get_req_id = getattr(client, "getReqId", None)
        if not callable(get_req_id):
            raise RuntimeError("IB client cannot reserve an order id")
        order_id = get_req_id()
        if not isinstance(order_id, int) or isinstance(order_id, bool) or order_id <= 0:
            raise RuntimeError("IB returned an invalid reserved order id")
        return order_id

    def create_contract(self, con_id: int, symbol: str = "", right: str = "C") -> Contract:
        """Create a Contract object for order placement."""
        contract = Contract()
        contract.conId = con_id
        contract.symbol = symbol
        contract.right = right
        contract.secType = "OPT"
        return contract

    def create_combo_contract(self, symbol: str, legs: List[tuple]) -> Contract:
        """BAG combo contract for multi-leg orders. legs = [(con_id, action), ...], ratio 1
        each. A debit vertical is [(long_con_id, "BUY"), (short_con_id, "SELL")]: BUY the
        combo to open, SELL the SAME combo to close -- both legs always trade atomically,
        so a close can never orphan the short leg."""
        from exitmgr.ibkr import ComboLeg
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "BAG"
        contract.currency = "USD"
        contract.exchange = "SMART"
        contract.comboLegs = [
            ComboLeg(conId=cid, ratio=1, action=action, exchange="SMART")
            for cid, action in legs
        ]
        return contract

    def create_limit_order(self, action: str, total_quantity: int, limit_price: float) -> Order:
        """Create a LIMIT order."""
        order = Order()
        order.action = action  # "BUY" or "SELL"
        order.orderType = "LMT"
        order.totalQuantity = total_quantity
        order.lmtPrice = limit_price
        order.tif = "DAY"  # Day order
        return order

    def create_market_order(self, action: str, total_quantity: int) -> Order:
        """Create a MARKET order (for exits, so a triggered stop/target always fills)."""
        order = Order()
        order.action = action  # "BUY" or "SELL"
        order.orderType = "MKT"
        order.totalQuantity = total_quantity
        order.tif = "DAY"
        return order
