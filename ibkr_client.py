"""
IBKR TWS/Gateway API client wrapper.

Manages the connection lifecycle and provides a clean interface
for requesting market data via the ibapi EClient/EWrapper.
"""

import threading
import time
import logging
from typing import Optional

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

logger = logging.getLogger(__name__)


class IBKRClient(EWrapper, EClient):
    """
    Combined EWrapper + EClient that stores incoming data and
    exposes a simple synchronous interface for the screener.
    """

    def __init__(self, host: str, port: int, client_id: int):
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        self.host = host
        self.port = port
        self.client_id = client_id

        self._connected = threading.Event()
        self._next_req_id = 1
        self._req_id_lock = threading.Lock()

        # Callbacks registered per request id: req_id -> callable
        self._callbacks: dict = {}

        # Chain of option expirations received from reqSecDefOptParams
        self.option_params: dict = {}  # symbol -> {"expirations": [...], "strikes": [...]}
        self._option_params_event: dict = {}  # symbol -> Event

        # Tick data per req_id: req_id -> {"bid": float, "ask": float, "last": float}
        self.tick_data: dict = {}
        self._tick_events: dict = {}  # req_id -> Event

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def connect_and_run(self) -> None:
        """Connect to TWS/Gateway and start the message loop in a daemon thread."""
        self.connect(self.host, self.port, self.client_id)
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        if not self._connected.wait(timeout=10):
            raise ConnectionError(
                f"Could not connect to IBKR at {self.host}:{self.port} "
                f"(client_id={self.client_id}). "
                "Make sure TWS / IB Gateway is running and API connections are enabled."
            )
        logger.info("Connected to IBKR (client_id=%d)", self.client_id)

    def disconnect_clean(self) -> None:
        self.disconnect()
        logger.info("Disconnected from IBKR")

    def next_req_id(self) -> int:
        with self._req_id_lock:
            rid = self._next_req_id
            self._next_req_id += 1
            return rid

    # ------------------------------------------------------------------
    # EWrapper callbacks – connection
    # ------------------------------------------------------------------

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        super().nextValidId(orderId)
        self._next_req_id = max(self._next_req_id, orderId)
        self._connected.set()

    def error(self, reqId: int, errorCode: int, errorString: str, advancedOrderRejectJson: str = "") -> None:  # noqa: N802
        # Suppress informational codes (2104, 2106, 2158 = market data farm)
        info_codes = {2104, 2106, 2158, 2119, 2108, 2107, 10167}
        if errorCode in info_codes:
            logger.debug("IBKR info [%d]: %s", errorCode, errorString)
            return
        logger.warning("IBKR error req=%d code=%d: %s", reqId, errorCode, errorString)
        # Unblock any waiting event for this req
        if reqId in self._tick_events:
            self._tick_events[reqId].set()

    # ------------------------------------------------------------------
    # Option parameters (expirations + strikes)
    # ------------------------------------------------------------------

    def request_option_params(self, symbol: str, exchange: str = "SMART", sec_type: str = "STK") -> dict:
        """
        Returns {"expirations": sorted list of "YYYYMMDD" strings,
                 "strikes": sorted list of floats}
        for the given underlying symbol.
        """
        req_id = self.next_req_id()
        event = threading.Event()
        self._option_params_event[symbol] = event
        self.option_params[symbol] = {"expirations": [], "strikes": []}

        # underConId = 0 means look it up by symbol
        self.reqSecDefOptParams(req_id, symbol, "", sec_type, 0)

        if not event.wait(timeout=15):
            logger.warning("Timeout waiting for option params for %s", symbol)
        return self.option_params[symbol]

    def securityDefinitionOptionParameter(  # noqa: N802
        self,
        reqId: int,  # noqa: N803
        exchange: str,
        underlyingConId: int,  # noqa: N803
        tradingClass: str,  # noqa: N803
        multiplier: str,
        expirations,
        strikes,
    ) -> None:
        # We want SMART exchange data preferably
        for symbol, data in self.option_params.items():
            if exchange in ("SMART", "CBOE"):
                data["expirations"] = sorted(expirations)
                data["strikes"] = sorted(strikes)
                if symbol in self._option_params_event:
                    self._option_params_event[symbol].set()
                break

    def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:  # noqa: N802
        # Fire all pending events in case we only got non-SMART exchanges
        for symbol, event in self._option_params_event.items():
            event.set()

    # ------------------------------------------------------------------
    # Market data (bid / ask snapshot)
    # ------------------------------------------------------------------

    def request_option_snapshot(self, contract: Contract, timeout: float = 5.0) -> dict:
        """
        Request a market-data snapshot for a single option contract.
        Returns {"bid": float, "ask": float, "last": float, "mid": float}.
        """
        req_id = self.next_req_id()
        event = threading.Event()
        self._tick_events[req_id] = event
        self.tick_data[req_id] = {"bid": float("nan"), "ask": float("nan"), "last": float("nan")}

        # snapshot=True → one-shot quote, no subscription
        self.reqMktData(req_id, contract, "", True, False, [])

        event.wait(timeout=timeout)
        self.cancelMktData(req_id)

        data = self.tick_data.pop(req_id, {})
        self._tick_events.pop(req_id, None)

        bid = data.get("bid", float("nan"))
        ask = data.get("ask", float("nan"))
        mid = (bid + ask) / 2 if not (bid != bid or ask != ask) else float("nan")
        data["mid"] = mid
        return data

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib) -> None:  # noqa: N802
        """
        Tick types relevant to us:
          1 = BID, 2 = ASK, 4 = LAST, 9 = CLOSE, 66 = DELAYED_BID, 67 = DELAYED_ASK
        """
        if reqId not in self.tick_data:
            return
        if tickType in (1, 66):
            self.tick_data[reqId]["bid"] = price if price > 0 else float("nan")
        elif tickType in (2, 67):
            self.tick_data[reqId]["ask"] = price if price > 0 else float("nan")
        elif tickType in (4, 68):
            self.tick_data[reqId]["last"] = price if price > 0 else float("nan")

        # Fire the event once we have both bid and ask (or they remain nan)
        d = self.tick_data[reqId]
        if reqId in self._tick_events:
            if not (d["bid"] != d["bid"]) and not (d["ask"] != d["ask"]):
                self._tick_events[reqId].set()

    def tickSnapshotEnd(self, reqId: int) -> None:  # noqa: N802
        if reqId in self._tick_events:
            self._tick_events[reqId].set()
