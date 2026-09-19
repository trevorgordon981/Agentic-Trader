"""Public API contract; production-derived narrative omitted."""

import asyncio
import copy
import math
import os
import time
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, replace

from exitmgr.ibkr import IB, Contract, Position, Order


_SUPPORTED_IB_ASYNC_SNAPSHOT_VERSIONS = frozenset({"2.1.0"})


def _record_snapshot_quote_times(ticker):
    """Public API contract; production-derived narrative omitted."""
    for tick in getattr(ticker, "ticks", ()):
        kind = getattr(tick, "tickType", None)
        side = "bid" if kind in (1, 66) else "ask" if kind in (2, 67) else None
        stamp = getattr(tick, "time", None)
        if side and isinstance(stamp, datetime) and stamp.tzinfo is not None:
            setattr(ticker, "_exitmgr_" + side + "_observed_utc", stamp.astimezone(timezone.utc).isoformat())


def _positive_finite_quote_value(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


@dataclass
class PositionData:
    """Public API contract; production-derived narrative omitted."""
    con_id: int
    symbol: str
    right: str
    quantity: int
    avg_cost: Optional[float]
    expiry: str = ""







    sec_type: str = "OPT"
    strike: float = 0.0

    @property
    def is_short(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        return self.quantity < 0


def _option_cost_per_share(position) -> Optional[float]:
    """Public API contract; production-derived narrative omitted."""
    contract = position.contract
    if str(getattr(contract, "secType", "") or "").upper() != "OPT":
        return None
    multiplier = getattr(contract, "multiplier", None)
    cost = getattr(position, "avgCost", None)
    if isinstance(multiplier, bool) or isinstance(cost, bool):
        return None
    try:
        multiplier, cost = float(multiplier), float(cost)
    except (TypeError, ValueError, OverflowError):
        return None
    if multiplier != 100 or not math.isfinite(cost) or cost == 0:
        return None
    return cost / multiplier


@dataclass
class OrderData:
    """Public API contract; production-derived narrative omitted."""
    con_id: int
    order_id: int
    remaining: int
    limit_price: Optional[float] = None
    perm_id: int = 0
    client_id: Optional[int] = None
    order_ref: Optional[str] = None
    status: Optional[str] = None


    order_ids: Tuple[int, ...] = ()
    order_count: int = 1

    @property
    def is_duplicated(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        return self.order_count > 1


class OrderBookUnreadable(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


class OrderNotTransmittedError(RuntimeError):
    """Public API contract; production-derived narrative omitted."""


@dataclass(frozen=True)
class OrderView:
    """Public API contract; production-derived narrative omitted."""

    orders: Dict[int, OrderData]
    readable: bool
    error: Optional[str] = None

    def __bool__(self):
        raise TypeError(
            "OrderView has no truth value -- check .readable explicitly. An unreadable "
            "open-order book is UNKNOWN, never empty.")

    @classmethod
    def known(cls, orders: Dict[int, OrderData]) -> "OrderView":
        return cls(orders=dict(orders or {}), readable=True)

    @classmethod
    def unknown(cls, error: str) -> "OrderView":

        return cls(orders={}, readable=False, error=error or "open-order read failed")

    def as_state_dicts(self) -> Dict[int, dict]:
        """Public API contract; production-derived narrative omitted."""
        if not self.readable:
            raise OrderBookUnreadable(self.error or "open-order book unreadable")
        return {
            od.con_id: {"order_id": od.order_id, "remaining": od.remaining,
                        "limit_price": getattr(od, "limit_price", None),
                        "perm_id": getattr(od, "perm_id", 0),
                        "client_id": getattr(od, "client_id", None),
                        "order_ref": getattr(od, "order_ref", None),
                        "status": getattr(od, "status", None),




                        "order_ids": list(getattr(od, "order_ids", None) or (od.order_id,)),


                        "order_count": int(getattr(od, "order_count", 1))}
            for od in self.orders.values()
        }


class IBConnection:
    """Public API contract; production-derived narrative omitted."""

    def __init__(self, host: str, port: int, client_id: int, market_data_type: int = 3,
                 *, startup_completed_orders: bool = True):
        if type(startup_completed_orders) is not bool:
            raise ValueError("startup_completed_orders must be a boolean")
        self.startup_completed_orders = startup_completed_orders
        self.host = host
        self.port = port
        self.client_id = client_id


        self._base_client_id = int(client_id) if str(client_id).isdigit() else 0





        self._rotation_idx: Optional[int] = None




        self._client_id_in_use = False


        self.market_data_type = market_data_type
        self.ib: Optional[IB] = None
        self._connected = False



        self._uplink_ok = True


        self._link_fault = False



        self._connection_generation = 0
        self._portfolio_observations: Dict[int, dict] = {}
        self._price_events_accepted = False



        self._qualified_quote_contracts: Dict[int, Contract] = {}
        self._invalid_quote_con_ids: Dict[int, float] = {}



        self._log_throttle: Dict[str, dict] = {}

    def _log_limited(self, key: str, level: str, message: str,
                     interval_s: float = 300.0) -> None:
        """Public API contract; production-derived narrative omitted."""
        now = time.monotonic()
        previous = self._log_throttle.get(key)
        if previous and now - float(previous["last"]) < float(interval_s):
            previous["suppressed"] = int(previous.get("suppressed", 0)) + 1
            return
        suffix = ""
        if previous and previous.get("suppressed"):
            suffix = f" ({int(previous['suppressed'])} identical repeats suppressed)"
        print(f"[{level}] {message}{suffix}")
        self._log_throttle[key] = {"last": now, "suppressed": 0}

    async def _snapshot_tickers(self, contracts: List[Contract],
                                timeout_s: float) -> List[Any]:
        """Public API contract; production-derived narrative omitted."""
        wrapper = self.ib.wrapper
        client = self.ib.client
        try:
            installed = importlib_metadata.version("ib_async")
        except importlib_metadata.PackageNotFoundError as exc:
            raise RuntimeError("ib_async distribution identity is unavailable") from exc
        if installed not in _SUPPORTED_IB_ASYNC_SNAPSHOT_VERSIONS:
            raise RuntimeError(
                "unsupported ib_async snapshot lifecycle %s (supported: %s)" %
                (installed, ", ".join(sorted(_SUPPORTED_IB_ASYNC_SNAPSHOT_VERSIONS))))
        required_wrapper_methods = ("startReq", "_endReq", "startTicker", "endTicker")
        required_wrapper_maps = (
            "_futures", "_results", "_reqId2Contract", "reqId2Ticker",
            "ticker2ReqId", "tickers")
        required_client_methods = ("getReqId", "reqMktData", "cancelMktData")
        missing = ["wrapper.%s" % name for name in required_wrapper_methods
                   if not callable(getattr(wrapper, name, None))]
        missing += ["wrapper.%s" % name for name in required_wrapper_maps
                    if not isinstance(getattr(wrapper, name, None), dict)]
        missing += ["client.%s" % name for name in required_client_methods
                    if not callable(getattr(client, name, None))]
        if missing:
            raise RuntimeError("unsupported ib_async snapshot shape: missing " +
                               ", ".join(missing))
        req_ids: List[int] = []
        futures: List[asyncio.Future] = []
        tickers: List[Any] = []
        contract_cache_keys: List[int] = []
        completed = False
        try:
            for contract in contracts:




                cache_key = hash(contract)
                wrapper.tickers.pop(cache_key, None)
                contract_cache_keys.append(cache_key)
                req_id = client.getReqId()
                req_ids.append(req_id)
                futures.append(wrapper.startReq(req_id, contract))
                ticker = wrapper.startTicker(req_id, contract, "snapshot")
                tickers.append(ticker)
                update_event = getattr(ticker, "updateEvent", None)
                if update_event is not None:
                    update_event += _record_snapshot_quote_times
                client.reqMktData(req_id, contract, "", True, False, [])




            timed_out = False
            try:
                await asyncio.wait_for(
                    asyncio.gather(*futures, return_exceptions=True), float(timeout_s))
            except asyncio.TimeoutError:
                timed_out = True
            successful = [ticker for ticker, future in zip(tickers, futures)
                          if future.done() and not future.cancelled()
                          and future.exception() is None]
            if len(successful) != len(tickers):
                self._log_limited(
                    "quote-snapshot-partial", "WARN",
                    f"snapshot batch completed {len(successful)}/{len(tickers)} contracts; "
                    "unfinished or failed contracts remain unavailable")
            if not successful:
                if timed_out:
                    raise asyncio.TimeoutError()
                errors = [future.exception() for future in futures
                          if future.done() and not future.cancelled()
                          and future.exception() is not None]
                if errors:
                    raise errors[0]
            completed = not timed_out
            return successful
        finally:



            pending = getattr(wrapper, "_futures", {})
            if not completed:
                for req_id in req_ids:
                    if req_id in pending:
                        try:
                            client.cancelMktData(req_id)
                        except Exception:
                            pass


            for req_id in req_ids:
                if req_id in getattr(wrapper, "_futures", {}):
                    try:
                        wrapper._endReq(req_id)
                    except Exception:
                        pass
            for ticker in tickers:
                update_event = getattr(ticker, "updateEvent", None)
                if update_event is not None:
                    update_event -= _record_snapshot_quote_times
                try:
                    wrapper.endTicker(ticker, "snapshot")
                except Exception:
                    pass


            for req_id in req_ids:
                wrapper.reqId2Ticker.pop(req_id, None)
                wrapper._reqId2Contract.pop(req_id, None)
            for cache_key, ticker in zip(contract_cache_keys, tickers):
                if wrapper.tickers.get(cache_key) is ticker:
                    wrapper.tickers.pop(cache_key, None)

    def _invalidate_price_observations(self) -> None:
        """Public API contract; production-derived narrative omitted."""


        self._connection_generation += 1
        self._price_events_accepted = False
        self._portfolio_observations.clear()
        self._qualified_quote_contracts.clear()
        self._invalid_quote_con_ids.clear()

    async def connect(self, retries: int = 0, retry_delay: float = 30.0, force: bool = False) -> bool:
        """Public API contract; production-derived narrative omitted."""


        if force:
            self._connected = False
            try:
                if self.ib:
                    self.ib.disconnect()
            except Exception:
                pass
            self.ib = None





            self.client_id = self._next_rotation_id()
            n = self._record_rotation()
            print(f"[INFO] reconnect: rotating to fresh clientId={self.client_id} "
                  f"(avoids Error 326; rotation {n} today)")
        if self._connected:
            return True

        attempt = 0
        while True:
            self._invalidate_price_observations()
            self.ib = IB()



            self._price_events_accepted = True







            try:
                self.ib.errorEvent += self._on_error
            except Exception as _e:
                print(f"[WARN] could not subscribe errorEvent: {_e}")
            try:
                self.ib.disconnectedEvent += self._on_disconnected
            except Exception as _e:
                print(f"[WARN] could not subscribe disconnectedEvent: {_e}")
            try:
                self.ib.updatePortfolioEvent += self._on_update_portfolio
            except Exception as _e:
                print(f"[WARN] could not subscribe updatePortfolioEvent: {_e}")
            try:



                _CONNECT_BACKSTOP_S = 30
                startup_options = {}
                if not self.startup_completed_orders:


                    from exitmgr.ibkr import BACKEND
                    if BACKEND != "ib_async":
                        raise RuntimeError("entry startup selection requires ib_async")
                    from ib_async import StartupFetch
                    from ib_async.ib import StartupFetchALL
                    startup_options["fetchFields"] = (
                        StartupFetchALL & ~StartupFetch.ORDERS_COMPLETE)
                await asyncio.wait_for(self.ib.connectAsync(
                    host=self.host,
                    port=self.port,
                    clientId=self.client_id,
                    timeout=10,
                    raiseSyncErrors=True,
                    **startup_options,
                ), _CONNECT_BACKSTOP_S)
                self._connected = True
                self._uplink_ok = True
                self._link_fault = False
                self._price_events_accepted = True
                self._client_id_in_use = False



                self.ib.reqMarketDataType(self.market_data_type)
                print(f"[INFO] Connected to IB at {self.host}:{self.port} "
                      f"(client_id={self.client_id}, market_data_type={self.market_data_type})")
                return True
            except Exception as e:
                print(f"[ERROR] Failed to connect to IB (attempt {attempt+1}/{retries+1}): {str(e) or type(e).__name__}")
                self._connected = False
                self._price_events_accepted = False
                try:
                    if self.ib:
                        self.ib.disconnect()
                except Exception:
                    pass
                self.ib = None
                if attempt >= retries:
                    return False
                attempt += 1














                if force and self._client_id_in_use:
                    self._client_id_in_use = False
                    _prev = self.client_id
                    self.client_id = self._next_rotation_id()
                    _n = self._record_rotation()
                    print(f"[INFO] clientId {_prev} refused as already-in-use; retrying "
                          f"immediately on {self.client_id} (rotation {_n} today)")
                    continue
                print(f"[INFO] retrying IB connect in {retry_delay:.0f}s ...")
                await asyncio.sleep(retry_delay)

    async def disconnect(self) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            if self.ib:
                self.ib.disconnect()
                if self._connected:
                    print("[INFO] Disconnected from IB")
        except Exception as e:
            print(f"[WARN] Error during disconnect: {e}")
        finally:


            self._connected = False
            self._uplink_ok = False
            self._link_fault = True
            self._invalidate_price_observations()
            self.ib = None

    def _on_update_portfolio(self, item) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            if not self._price_events_accepted:
                return
            contract = getattr(item, "contract", None)
            cid = int(getattr(contract, "conId", 0) or 0)
            if cid <= 0:
                return
            qty = float(getattr(item, "position", 0) or 0)
            px = float(getattr(item, "marketPrice", float("nan")))



            if qty == 0 or not math.isfinite(px) or px <= 0:
                self._portfolio_observations.pop(cid, None)
                return
            self._portfolio_observations[cid] = {
                "price": px,
                "observed_monotonic": time.monotonic(),
                "observed_utc": datetime.now(timezone.utc).isoformat(),
                "generation": self._connection_generation,
            }
        except Exception:
            return

    def portfolio_mark_observations(self, max_age_s: float = 45.0) -> Dict[int, dict]:
        """Public API contract; production-derived narrative omitted."""
        now = time.monotonic()
        out = {}
        for cid, row in tuple(self._portfolio_observations.items()):
            try:
                age = now - float(row["observed_monotonic"])
                if (row.get("generation") == self._connection_generation
                        and 0 <= age <= float(max_age_s)):
                    out[int(cid)] = dict(row, age_s=age)
            except (TypeError, ValueError, KeyError):
                continue
        return out

    def _on_error(self, reqId, errorCode, errorString, contract=None):
        """Public API contract; production-derived narrative omitted."""
        try:
            if errorCode in (1100, 1300, 2110):
                self._uplink_ok = False
                self._invalidate_price_observations()
                print(f"[WARN] IBKR uplink DOWN (code {errorCode}): {errorString}")


            elif errorCode in (1102, 2104, 2106, 2158):
                self._uplink_ok = True
                self._price_events_accepted = True
                print(f"[INFO] IBKR uplink RESTORED (code {errorCode}): {errorString}")
            elif errorCode == 1101:








                self._uplink_ok = True
                self._price_events_accepted = True
                print(f"[INFO] IBKR uplink RESTORED, data subs dropped (code {errorCode}): "
                      f"{errorString} -- quotes are re-requested per cycle, continuing")
            elif errorCode == 326:



                self._client_id_in_use = True
                print(f"[WARN] clientId {self.client_id} already in use at the gateway "
                      f"(code 326): {errorString}")
            elif errorCode == 200:

                cid = int(getattr(contract, "conId", 0) or 0)
                if cid > 0:
                    self._qualified_quote_contracts.pop(cid, None)
                    self._invalid_quote_con_ids[cid] = time.monotonic() + 60.0
        except Exception:
            pass

    def _on_disconnected(self, *args) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            self._connected = False
            self._uplink_ok = False
            self._link_fault = True
            self._invalidate_price_observations()
            print("[WARN] IB Gateway socket disconnected; link invalidated")
        except Exception:
            pass

    _ROTATION_POOL_SIZE = 4







    _DIRECT_POOL_CEILING = 8990




















    _FOLD_FLOOR = 10000
    _FOLD_DOMAIN = 10000



    _ROTATION_COUNT_ENV = "EXITMGR_LINK_ROTATIONS_PATH"
    _ROTATION_COUNT_DEFAULT = os.path.expanduser("~/.local/var/exitmgr/link-rotations.json")

    @property
    def _ROTATION_COUNT_PATH(self) -> str:
        return os.environ.get(self._ROTATION_COUNT_ENV) or self._ROTATION_COUNT_DEFAULT

    def _rotation_pool(self):
        """Public API contract; production-derived narrative omitted."""
        start = 4000 + (self._base_client_id * 10)
        if start > self._DIRECT_POOL_CEILING:
            start = (self._FOLD_FLOOR
                     + (self._base_client_id % self._FOLD_DOMAIN) * self._ROTATION_POOL_SIZE)
        return [start + i for i in range(self._ROTATION_POOL_SIZE)]






    _ROTATION_CURSOR_ABSENT_IDX = 1

    _ROTATION_CURSOR_ENV = "EXITMGR_ROTATION_CURSOR_PATH"

    @property
    def _ROTATION_CURSOR_PATH(self) -> str:
        """Public API contract; production-derived narrative omitted."""
        override = os.environ.get(self._ROTATION_CURSOR_ENV)
        if override:
            return override
        return os.path.join(os.path.dirname(self._ROTATION_COUNT_PATH),
                            "link-rotation-cursor-%d.json" % self._base_client_id)

    def _load_rotation_cursor(self) -> int:
        """Public API contract; production-derived narrative omitted."""
        try:
            import json as _json
            with open(self._ROTATION_CURSOR_PATH) as fh:
                data = _json.load(fh)
            if int(data["base"]) != self._base_client_id:
                raise ValueError("cursor belongs to base %r, not %r"
                                 % (data.get("base"), self._base_client_id))
            idx = int(data["next_idx"])
            if idx < 0:
                raise ValueError("negative cursor %r" % (idx,))
            return idx % self._ROTATION_POOL_SIZE
        except Exception:
            return self._ROTATION_CURSOR_ABSENT_IDX % self._ROTATION_POOL_SIZE

    def _save_rotation_cursor(self, next_idx: int, used_id: int) -> None:
        """Public API contract; production-derived narrative omitted."""
        try:
            import json as _json
            path = self._ROTATION_CURSOR_PATH
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                _json.dump({"base": self._base_client_id,
                            "next_idx": int(next_idx) % self._ROTATION_POOL_SIZE,
                            "last_id": int(used_id),
                            "pid": os.getpid(),
                            "ts": datetime.now(timezone.utc).isoformat()}, fh)
            os.replace(tmp, path)
        except Exception as _e:
            print(f"[WARN] could not persist clientId rotation cursor: {_e} "
                  f"-- rotation degrades to per-process for this run")

    def _next_rotation_id(self) -> int:
        """Public API contract; production-derived narrative omitted."""
        pool = self._rotation_pool()
        if self._rotation_idx is None:
            self._rotation_idx = self._load_rotation_cursor()
        cid = pool[self._rotation_idx % len(pool)]
        self._rotation_idx = (self._rotation_idx + 1) % len(pool)


        self._save_rotation_cursor(self._rotation_idx, cid)
        return cid

    def _record_rotation(self) -> int:
        """Public API contract; production-derived narrative omitted."""
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
        """Public API contract; production-derived narrative omitted."""
        return self._link_usable()

    def _link_usable(self) -> bool:
        """Public API contract; production-derived narrative omitted."""
        try:
            return bool(self._connected and self.ib and self.ib.isConnected()
                        and self._uplink_ok and not self._link_fault)
        except Exception:
            return False

    def _require_link(self):
        """Public API contract; production-derived narrative omitted."""
        if not self._link_usable():
            self._link_fault = True
            raise RuntimeError("Not connected to IB")

    async def ensure_connected(self, probe: bool = True, timeout: float = 5.0) -> bool:
        """Public API contract; production-derived narrative omitted."""
        if not self.is_healthy():
            return False
        if not probe:
            return True
        loop = asyncio.get_running_loop()
        lock = getattr(self, "_health_probe_lock", None)
        if lock is None:
            lock = self._health_probe_lock = asyncio.Lock()
        ib = self.ib
        generation = self._connection_generation
        sent = False

        def same_connection():
            return self.ib is ib and self._connection_generation == generation

        async def roundtrip():
            nonlocal sent
            async with lock:
                if not same_connection() or not self.is_healthy():
                    return False
                previous = getattr(self, "_last_health_probe_reply", None)
                if previous is not None and previous[0] is ib and previous[1] == generation:
                    remaining = 1.2 - (loop.time() - previous[2])
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                if not same_connection() or not self.is_healthy():
                    return False
                sent = True
                await ib.reqCurrentTimeAsync()
                if not same_connection() or not self.is_healthy():
                    return False
                self._last_health_probe_reply = (ib, generation, loop.time())
                return True

        try:

            return await asyncio.wait_for(roundtrip(), timeout=timeout)
        except asyncio.CancelledError:


            if sent and same_connection():
                self._link_fault = True
                self._invalidate_price_observations()
            raise
        except Exception as e:
            print(f"[WARN] liveness probe failed (treating link as DOWN): {e}")
            if same_connection():
                self._uplink_ok = False
                self._link_fault = True
                self._invalidate_price_observations()
            return False

    async def reconnect(self, retries: int = 3, retry_delay: float = 10.0) -> bool:
        """Public API contract; production-derived narrative omitted."""
        print("[WARN] forcing IBKR reconnect ...")
        return await self.connect(retries=retries, retry_delay=retry_delay, force=True)

    @staticmethod
    def _str_field(obj, name: str) -> str:
        """Public API contract; production-derived narrative omitted."""
        v = getattr(obj, name, None)
        return v if isinstance(v, str) else ""

    @staticmethod
    def _num_field(obj, name: str) -> float:
        """Public API contract; production-derived narrative omitted."""
        v = getattr(obj, name, None)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return 0.0
        return float(v) if v == v else 0.0

    async def get_positions(self, include_short: bool = False,
                            include_stock: bool = False) -> Dict[int, PositionData]:
        """Public API contract; production-derived narrative omitted."""
        self._require_link()







        try:
            positions = await asyncio.wait_for(self.ib.reqPositionsAsync(), 30)
        except (asyncio.TimeoutError, ConnectionError) as exc:




            self._link_fault = True
            self._invalidate_price_observations()
            print(f"[WARN] positions request failed ({type(exc).__name__}); "
                  "link invalidated for reconnect; positions remain UNKNOWN")
            raise

        result: Dict[int, PositionData] = {}
        for pos in positions:



            contract = pos.contract
            position_qty = pos.position



            if (hasattr(contract, 'right') and contract.right in ('C', 'P') and position_qty > 0):
                con_id = contract.conId
                result[con_id] = PositionData(
                    con_id=con_id,
                    symbol=contract.symbol if hasattr(contract, 'symbol') else "",
                    right=contract.right,
                    quantity=position_qty,
                    avg_cost=_option_cost_per_share(pos),
                    expiry=getattr(contract, 'lastTradeDateOrContractMonth', '') or '',
                    sec_type=(self._str_field(contract, 'secType').upper() or "OPT"),
                    strike=self._num_field(contract, 'strike'),
                )
                continue

            if include_short and position_qty < 0:





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
                    quantity=position_qty,
                    avg_cost=_option_cost_per_share(pos),
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
                    right="",
                    quantity=position_qty,
                    avg_cost=pos.avgCost if hasattr(pos, 'avgCost') else 0.0,
                    expiry="",
                    sec_type="STK",
                    strike=0.0,
                )

        return result

    async def get_open_orders(self, short_leg_con_ids=None) -> Dict[int, OrderData]:
        """Public API contract; production-derived narrative omitted."""
        self._require_link()







        trades = await asyncio.wait_for(self.ib.reqAllOpenOrdersAsync(), 30)





        if trades is None:
            raise OrderBookUnreadable("reqAllOpenOrdersAsync returned None")
        _short_legs = {int(c) for c in (short_leg_con_ids or [])}

        result: Dict[int, OrderData] = {}
        for t in trades:
            order = getattr(t, "order", None)
            contract = getattr(t, "contract", None)
            status = getattr(t, "orderStatus", None)
            if order is None or contract is None:
                continue


            con_id = self._order_key_con_id(contract)
            if con_id is None:
                continue
            _action = getattr(order, "action", "")




            if _action != "SELL":
                if not (_action == "BUY" and con_id in _short_legs):
                    continue


            remaining = getattr(status, "remaining", None) if status is not None else None
            if not (isinstance(remaining, (int, float)) and remaining == remaining):
                total = getattr(order, "totalQuantity", 0) or 0
                filled = getattr(status, "filled", 0) if status is not None else 0
                if not isinstance(filled, (int, float)) or filled != filled:
                    filled = 0
                remaining = max(0, total - filled)

            _oid = getattr(order, "orderId", 0) or 0
            _prior = result.get(con_id)
            if _prior is None:
                result[con_id] = OrderData(
                    con_id=con_id,
                    order_id=_oid,
                    remaining=int(remaining),
                    limit_price=getattr(order, "lmtPrice", None),
                    perm_id=getattr(order, "permId", 0) or 0,
                    client_id=(getattr(order, "clientId", None)
                               if isinstance(getattr(order, "clientId", None), int) else None),
                    order_ref=getattr(order, "orderRef", None) or None,
                    status=getattr(status, "status", None) if status is not None else None,
                    order_ids=(_oid,),
                    order_count=1,
                )
                continue









            _ids = tuple(sorted(set(_prior.order_ids or (_prior.order_id,)) | {_oid}))
            result[con_id] = replace(
                _prior,
                remaining=int(_prior.remaining) + int(remaining),
                order_ids=_ids,
                order_count=len(_ids),
            )

        return result

    async def get_open_orders_view(self, short_leg_con_ids=None) -> OrderView:
        """Public API contract; production-derived narrative omitted."""
        try:
            orders = await self.get_open_orders(short_leg_con_ids=short_leg_con_ids)
        except Exception as e:
            return OrderView.unknown(str(e) or f"timed out ({type(e).__name__})")
        if orders is None:
            return OrderView.unknown("open-order read returned None")
        return OrderView.known(orders)

    @staticmethod
    def _order_key_con_id(contract) -> Optional[int]:
        """Public API contract; production-derived narrative omitted."""
        if getattr(contract, "secType", "") == "BAG":
            legs = getattr(contract, "comboLegs", None) or []
            for leg in legs:
                if getattr(leg, "action", "") == "BUY" and getattr(leg, "conId", None):
                    return int(leg.conId)

            for leg in legs:
                if getattr(leg, "conId", None):
                    return int(leg.conId)
            return None
        cid = getattr(contract, "conId", None)
        return int(cid) if cid else None

    async def fetch_quotes(self, con_ids: List[int]) -> Dict[int, dict]:
        """Public API contract; production-derived narrative omitted."""
        self._require_link()
        request_generation = self._connection_generation



        wanted = []
        for raw in con_ids or []:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in wanted:
                wanted.append(cid)
        if not wanted:
            return {}

        now = time.monotonic()
        contracts = [self._qualified_quote_contracts[cid] for cid in wanted
                     if cid in self._qualified_quote_contracts]
        missing = [cid for cid in wanted if cid not in self._qualified_quote_contracts
                   and now >= self._invalid_quote_con_ids.get(cid, 0.0)]
        raw_contracts = []
        for con_id in missing:
            contract = Contract()
            contract.conId = con_id
            raw_contracts.append(contract)








        _QUOTE_QUALIFY_TIMEOUT_S = 5






        _QUOTE_SNAPSHOT_TIMEOUT_S = 15
        if raw_contracts:
            try:
                qc = await asyncio.wait_for(
                    self.ib.qualifyContractsAsync(*raw_contracts), _QUOTE_QUALIFY_TIMEOUT_S)
                returned = {int(getattr(c, "conId", 0) or 0): c for c in (qc or [])
                            if int(getattr(c, "conId", 0) or 0) in missing}
                for cid, contract in returned.items():
                    self._qualified_quote_contracts[cid] = contract
                    self._invalid_quote_con_ids.pop(cid, None)
                    contracts.append(contract)


                for cid in set(missing) - set(returned):
                    self._invalid_quote_con_ids[cid] = time.monotonic() + 60.0
            except asyncio.TimeoutError:
                self._log_limited(
                    "quote-qualify-timeout", "WARN",
                    f"qualify timed out after {_QUOTE_QUALIFY_TIMEOUT_S}s in fetch_quotes; "
                    "unqualified contracts were not sent to market data")
            except Exception as e:
                self._log_limited(
                    f"quote-qualify-failed:{type(e).__name__}", "WARN",
                    f"qualify failed in fetch_quotes: {e}; unqualified contracts were not "
                    "sent to market data")
        if not contracts:
            return {}

        try:



            quote_observed_monotonic = time.monotonic()
            quote_observed_utc = datetime.now(timezone.utc).isoformat()
            tickers = await self._snapshot_tickers(contracts, _QUOTE_SNAPSHOT_TIMEOUT_S)
        except asyncio.TimeoutError:


            self._log_limited(
                "quote-snapshot-timeout", "WARN",
                f"snapshot quotes timed out after {_QUOTE_SNAPSHOT_TIMEOUT_S}s for "
                f"{len(contracts)} contracts; exact request ids were retired and broker marks "
                "remain available")
            return {}
        except Exception as e:
            self._log_limited(
                f"quote-snapshot-failed:{type(e).__name__}", "WARN",
                f"snapshot quotes failed ({e}); broker marks remain available")
            return {}




        if (request_generation != self._connection_generation
                or not self._link_usable()):
            self._log_limited(
                "quote-generation-discard", "WARN",
                "discarding quote batch observed across an IB connection-generation change")
            return {}

        result: Dict[int, dict] = {}
        for ticker in tickers:
            if ticker.contract and hasattr(ticker.contract, 'conId'):
                con_id = ticker.contract.conId


                bid = ticker.bid if hasattr(ticker, 'bid') else None
                ask = ticker.ask if hasattr(ticker, 'ask') else None
                last = ticker.last if hasattr(ticker, 'last') else None
                mark = ticker.mark if hasattr(ticker, 'mark') else None





                if mark is not None and not (mark != mark) and mark > 0:
                    price = mark
                elif bid is not None and ask is not None and bid > 0 and ask > 0 and not (bid != bid) and not (ask != ask):
                    price = (bid + ask) / 2.0
                elif last is not None and not (last != last) and last > 0:
                    price = last
                else:

                    print(f"[WARN] Skipping con_id={con_id} due to stale/NaN quote data")
                    continue



                g = getattr(ticker, "modelGreeks", None) or getattr(ticker, "lastGreeks", None)
                iv = getattr(g, "impliedVol", None) if g else None
                delta = getattr(g, "delta", None) if g else None



                def _fin(x):
                    return x if (x is not None and x == x) else None
                gamma = _fin(getattr(g, "gamma", None) if g else None)
                theta = _fin(getattr(g, "theta", None) if g else None)
                vega = _fin(getattr(g, "vega", None) if g else None)
                result[con_id] = {
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "mark": mark,
                    "price": price,
                    "iv": iv if (iv is not None and iv == iv) else None,
                    "delta": delta if (delta is not None and delta == delta) else None,
                    "gamma": gamma, "theta": theta, "vega": vega,
                    "min_tick": _positive_finite_quote_value(getattr(ticker, "minTick", None)),
                    "market_data_type": getattr(ticker, "marketDataType", None),
                    "underlying_price": _positive_finite_quote_value(getattr(g, "undPrice", None) if g else None),
                    "bid_observed_utc": getattr(ticker, "_exitmgr_bid_observed_utc", None),
                    "ask_observed_utc": getattr(ticker, "_exitmgr_ask_observed_utc", None),
                    "observed_monotonic": quote_observed_monotonic,
                    "observed_utc": quote_observed_utc,
                    "generation": request_generation,
                }

        return result

    async def place_order(
        self,
        contract: Contract,
        order: Order,
    ) -> Order:
        """Public API contract; production-derived narrative omitted."""




        if not await self.ensure_connected(probe=True, timeout=2.0):
            raise OrderNotTransmittedError(
                "IB link failed final pre-transmission probe (nothing sent)")
        self._require_link()


        from exitmgr.order_lock import order_mutation_lock
        with order_mutation_lock():
            trade = self.ib.placeOrder(contract, order)
        return trade

    def reserve_order_id(self) -> int:
        """Public API contract; production-derived narrative omitted."""
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
        """Public API contract; production-derived narrative omitted."""
        candidates = []
        for method_name in ("positions", "portfolio"):
            reader = getattr(self.ib, method_name, None)
            if callable(reader):
                try:
                    candidates.extend(getattr(row, "contract", None) for row in reader())
                except Exception:
                    continue
        candidates.append(self._qualified_quote_contracts.get(int(con_id)))
        source = next((item for item in candidates
                       if item is not None and getattr(item, "conId", None) == int(con_id)), None)
        if source is None:
            raise ValueError(f"cannot route close for con_id={con_id}: broker contract unknown")
        contract = copy.copy(source)
        actual_symbol = str(getattr(contract, "symbol", "") or "")
        actual_right = str(getattr(contract, "right", "") or "").upper()
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        currency = str(getattr(contract, "currency", "") or "").upper()
        exchange = str(getattr(contract, "exchange", "") or "")
        if ((symbol and actual_symbol != symbol)
                or (right and actual_right != str(right).upper())
                or actual_right not in {"C", "P"}
                or sec_type not in {"OPT", "FOP"} or not currency):
            raise ValueError(f"cannot route close for con_id={con_id}: broker identity mismatch")
        if not exchange:
            if sec_type != "OPT" or currency != "USD":
                raise ValueError(f"cannot route close for con_id={con_id}: exchange unknown")
            contract.exchange = "SMART"
        return contract

    def create_combo_contract(self, symbol: str, legs: List[tuple]) -> Contract:
        """Public API contract; production-derived narrative omitted."""
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
        """Public API contract; production-derived narrative omitted."""
        order = Order()
        order.action = action
        order.orderType = "LMT"
        order.totalQuantity = total_quantity
        order.lmtPrice = limit_price
        order.tif = "DAY"
        return order

    def create_market_order(self, action: str, total_quantity: int) -> Order:
        """Public API contract; production-derived narrative omitted."""
        order = Order()
        order.action = action
        order.orderType = "MKT"
        order.totalQuantity = total_quantity
        order.tif = "DAY"
        return order
