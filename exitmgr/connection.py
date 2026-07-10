"""IB connection and market data management using ib_async."""

import asyncio
from typing import Optional, Dict, List, Any
from dataclasses import dataclass

from exitmgr.ibkr import IB, Contract, Position, Order


@dataclass
class PositionData:
    """Normalized position data from IB."""
    con_id: int
    symbol: str
    right: str  # 'C' or 'P'
    quantity: int  # contracts (positive for long)
    avg_cost: float  # average cost per share
    expiry: str = ""  # option expiry YYYYMMDD


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
        # 1=live (needs paid subscription), 3=delayed (free; ~15min lag). Default delayed so an
        # unsubscribed account gets usable quotes instead of Error 10089 + NaN tickers.
        self.market_data_type = market_data_type
        self.ib: Optional[IB] = None
        self._connected = False
        # SELF-HEAL: tracks the gateway<->IBKR UPLINK, which a local isConnected() can NOT see.
        # On Error 1100 the uplink drops but our 127.0.0.1 TCP socket stays open, so
        # isConnected() lies True. The errorEvent handler flips this so is_healthy() is honest.
        self._uplink_ok = True

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
            import random as _rnd
            self.client_id = _rnd.randint(1000, 9000)
            print(f"[INFO] reconnect: rotating to fresh clientId={self.client_id} (avoids Error 326)")
        if self._connected:
            return True

        attempt = 0
        while True:
            self.ib = IB()
            try:
                await self.ib.connectAsync(
                    host=self.host,
                    port=self.port,
                    clientId=self.client_id,
                    timeout=10,
                )
                self._connected = True
                self._uplink_ok = True
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
                print(f"[ERROR] Failed to connect to IB (attempt {attempt+1}/{retries+1}): {e}")
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
        but data farm broken -> treat as not-ok. 1102 = connectivity RESTORED -> ok."""
        try:
            if errorCode in (1100, 1300, 2110):
                self._uplink_ok = False
                print(f"[WARN] IBKR uplink DOWN (code {errorCode}): {errorString}")
            elif errorCode == 1102:
                self._uplink_ok = True
                print(f"[INFO] IBKR uplink RESTORED (code {errorCode}): {errorString}")
        except Exception:
            pass

    def is_healthy(self) -> bool:
        """Cheap, non-blocking liveness: live socket AND uplink reported up."""
        return bool(self.ib and self.ib.isConnected() and self._uplink_ok)

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

    async def get_positions(self) -> Dict[int, PositionData]:
        """
        Fetch all current positions using reqPositionsAsync.
        Returns dict mapping con_id to PositionData.
        Only includes LONG option positions (calls AND puts, quantity > 0). Short legs of
        debit spreads are intentionally excluded -- they are managed as a unit via the
        journaled long leg (see manager.py spread handling).
        """
        if not self._connected or not self.ib:
            raise RuntimeError("Not connected to IB")

        positions = await self.ib.reqPositionsAsync()

        result: Dict[int, PositionData] = {}
        for pos in positions:
            # pos.contract should have conId, symbol, right
            # pos.position is the number of contracts (positive=long)
            # pos.avgCost is average cost per share
            contract = pos.contract
            position_qty = pos.position

            # Filter: long options only (calls and puts, quantity > 0)
            if (hasattr(contract, 'right') and contract.right in ('C', 'P') and position_qty > 0):
                con_id = contract.conId
                result[con_id] = PositionData(
                    con_id=con_id,
                    symbol=contract.symbol if hasattr(contract, 'symbol') else "",
                    right=contract.right,
                    quantity=position_qty,
                    avg_cost=pos.avgCost if hasattr(pos, 'avgCost') else 0.0,
                    expiry=getattr(contract, 'lastTradeDateOrContractMonth', '') or '',
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
        if not self._connected or not self.ib:
            raise RuntimeError("Not connected to IB")

        trades = await self.ib.reqAllOpenOrdersAsync()
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
        if not self._connected or not self.ib:
            raise RuntimeError("Not connected to IB")

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
        try:
            qc = await self.ib.qualifyContractsAsync(*contracts)
            contracts = [c for c in qc if getattr(c, "conId", None)] or contracts
        except Exception as e:
            print(f"[WARN] qualify failed in fetch_quotes: {e}")
        # Request tickers
        tickers = await self.ib.reqTickersAsync(*contracts)

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
        if not self._connected or not self.ib:
            raise RuntimeError("Not connected to IB")

        # ib_async: placeOrder is SYNCHRONOUS and returns a Trade immediately
        trade = self.ib.placeOrder(contract, order)
        return trade

    def reserve_order_id(self) -> int:
        """Reserve this client's next IB order id before transmission.

        ib_async normally performs the same allocation inside ``placeOrder``.  Exposing it one
        step earlier lets the exit manager fsync the exact client-scoped identity first.
        """
        if not self._connected or not self.ib:
            raise RuntimeError("Not connected to IB")
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
