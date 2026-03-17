"""
IBKR TWS/Gateway API client wrapper.

Manages the connection lifecycle and provides a clean interface
for requesting market data via the ibapi EClient/EWrapper.
"""

import math
import threading
import logging

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

logger = logging.getLogger(__name__)

# Informational message codes that are not errors (market data farm status, etc.)
_INFO_CODES = {2104, 2106, 2107, 2108, 2119, 2158, 10167}

# Fatal connection-level errors that should abort the connection attempt immediately.
_FATAL_CONNECT_CODES = {
    326,  # client id already in use
    507,  # bad message length / socket disconnect
}


class IBKRClient(EWrapper, EClient):
    """
    Combined EWrapper + EClient that stores incoming data and
    exposes a simple synchronous interface for the screener.
    """

    def __init__(self, host: str, port: int, client_id: int):
        if not host:
            raise ValueError(
                "IBKR_HOST is not configured. "
                "Set IBKR_HOST in your .env file (e.g. IBKR_HOST=127.0.0.1 for TWS)."
            )
        if not port:
            raise ValueError(
                "IBKR_PORT is not configured. "
                "Set IBKR_PORT in your .env file (e.g. IBKR_PORT=7497 for TWS paper)."
            )

        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)

        self.host = host
        self.port = port
        self.client_id = client_id

        # Keep immutable copies — ibapi resets self.host/self.port to None
        # on disconnect, so we need our own reference for error messages.
        self._host = host
        self._port = port

        self._connected = threading.Event()
        self._connect_error: str | None = None   # set by error() for fatal codes
        self._next_req_id = 1
        self._req_id_lock = threading.Lock()

        # Option params: symbol -> {"expirations": [...], "strikes": [...]}
        self.option_params: dict = {}
        self._option_params_event: dict = {}   # symbol -> Event
        self._req_id_to_symbol: dict = {}      # req_id -> symbol (for callback routing)

        # Tick data per req_id: req_id -> {"bid": float, "ask": float, "last": float}
        self.tick_data: dict = {}
        self._tick_events: dict = {}  # req_id -> Event

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def connect_and_run(self) -> None:
        """Connect to TWS/Gateway and start the message loop in a daemon thread."""
        self.connect(self._host, self._port, self.client_id)
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        if not self._connected.wait(timeout=10):
            raise ConnectionError(
                f"Could not connect to IBKR at {self._host}:{self._port} "
                f"(client_id={self.client_id}). "
                "Make sure TWS / IB Gateway is running and API connections are enabled."
            )
        if self._connect_error:
            raise ConnectionError(self._connect_error)
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
        if errorCode in _INFO_CODES:
            logger.debug("IBKR info [%d]: %s", errorCode, errorString)
            return
        logger.warning("IBKR error req=%d code=%d: %s", reqId, errorCode, errorString)
        # Fatal connection errors — fail fast instead of waiting for the 10s timeout
        if errorCode in _FATAL_CONNECT_CODES:
            self._connect_error = f"IBKR rejected connection (code {errorCode}): {errorString}"
            self._connected.set()
            return
        # Unblock any waiting event for this req
        if reqId in self._tick_events:
            self._tick_events[reqId].set()

    # ------------------------------------------------------------------
    # Option parameters (expirations + strikes)
    # ------------------------------------------------------------------

    def request_option_params(self, symbol: str, sec_type: str = "STK") -> dict:
        """
        Returns {"expirations": sorted list of "YYYYMMDD" strings,
                 "strikes": sorted list of floats}
        for the given underlying symbol.
        """
        req_id = self.next_req_id()
        event = threading.Event()
        self._option_params_event[symbol] = event
        self._req_id_to_symbol[req_id] = symbol
        self.option_params[symbol] = {"expirations": [], "strikes": []}

        # underConId = 0 means look it up by symbol; exchange "" = all exchanges
        self.reqSecDefOptParams(req_id, symbol, "", sec_type, 0)

        if not event.wait(timeout=15):
            logger.warning("Timeout waiting for option params for %s", symbol)

        self._option_params_event.pop(symbol, None)
        self._req_id_to_symbol.pop(req_id, None)
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
        symbol = self._req_id_to_symbol.get(reqId)
        if symbol is None:
            return
        if exchange not in ("SMART", "CBOE"):
            return
        data = self.option_params.get(symbol)
        if data is None:
            return
        data["expirations"] = sorted(expirations)
        data["strikes"] = sorted(strikes)
        if symbol in self._option_params_event:
            self._option_params_event[symbol].set()

    def securityDefinitionOptionParameterEnd(self, reqId: int) -> None:  # noqa: N802
        # Fire all pending events in case we only got non-SMART/CBOE exchanges
        for event in self._option_params_event.values():
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
        data["mid"] = (bid + ask) / 2 if not (math.isnan(bid) or math.isnan(ask)) else float("nan")
        return data

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib) -> None:  # noqa: N802
        """
        Tick types relevant to us:
          1 = BID, 2 = ASK, 4 = LAST, 66 = DELAYED_BID, 67 = DELAYED_ASK, 68 = DELAYED_LAST
        """
        d = self.tick_data.get(reqId)
        if d is None:
            return
        if tickType in (1, 66):
            d["bid"] = price if price > 0 else float("nan")
        elif tickType in (2, 67):
            d["ask"] = price if price > 0 else float("nan")
        elif tickType in (4, 68):
            d["last"] = price if price > 0 else float("nan")

        # Fire the event once we have both bid and ask
        if reqId in self._tick_events:
            if not math.isnan(d["bid"]) and not math.isnan(d["ask"]):
                self._tick_events[reqId].set()

    def tickSnapshotEnd(self, reqId: int) -> None:  # noqa: N802
        if reqId in self._tick_events:
            self._tick_events[reqId].set()
