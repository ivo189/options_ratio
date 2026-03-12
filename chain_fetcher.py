"""
Options chain fetcher.

Given a connected IBKRClient, a symbol and an expiration date, fetches
bid/ask quotes for every call strike and returns a structured DataFrame.
"""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import pandas as pd
from ibapi.contract import Contract

from ibkr_client import IBKRClient

logger = logging.getLogger(__name__)

# Maximum parallel market-data requests.
# IBKR limits simultaneous snapshot requests; keep this conservative.
MAX_WORKERS = 10


@dataclass
class OptionQuote:
    strike: float
    bid: float
    ask: float
    mid: float

    @property
    def spread(self) -> float:
        if math.isnan(self.bid) or math.isnan(self.ask):
            return float("nan")
        return self.ask - self.bid

    @property
    def is_valid(self) -> bool:
        """True when we have real bid and ask quotes."""
        return not (math.isnan(self.bid) or math.isnan(self.ask)) and self.ask > 0


def _make_option_contract(symbol: str, expiration: str, strike: float, right: str = "C") -> Contract:
    c = Contract()
    c.symbol = symbol
    c.secType = "OPT"
    c.exchange = "SMART"
    c.currency = "USD"
    c.lastTradeDateOrContractMonth = expiration
    c.strike = strike
    c.right = right
    c.multiplier = "100"
    return c


def _make_stk_contract(symbol: str) -> Contract:
    c = Contract()
    c.symbol = symbol
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


def fetch_chain(
    client: IBKRClient,
    symbol: str,
    expiration: str,
    right: str = "C",
    strikes: list[float] | None = None,
    timeout_per_strike: float = 5.0,
    underlying_price: float | None = None,
    otm_only: bool = True,
    strike_range: tuple[float, float] | None = None,
) -> pd.DataFrame:
    """
    Fetch bid/ask for every call (or put) strike for *symbol* on *expiration*.

    Parameters
    ----------
    client              : connected IBKRClient
    symbol              : underlying ticker, e.g. "SPY"
    expiration          : "YYYYMMDD"
    right               : "C" or "P"
    strikes             : explicit list of strikes to query (skip option-params request)
    timeout_per_strike  : seconds to wait for each quote
    underlying_price    : if provided and otm_only=True, filter OTM strikes
    otm_only            : skip deep ITM strikes (reduces requests)
    strike_range        : (low, high) absolute strike filter

    Returns
    -------
    DataFrame with columns: strike, bid, ask, mid, spread, valid
    Sorted ascending by strike.
    """
    if strikes is None:
        params = client.request_option_params(symbol)
        strikes = params.get("strikes", [])
        if not strikes:
            raise ValueError(f"No option parameters returned for {symbol}. Is it optionable?")

    strikes = sorted(strikes)

    # Apply filters to reduce the number of requests
    if strike_range:
        lo, hi = strike_range
        strikes = [s for s in strikes if lo <= s <= hi]

    if otm_only and underlying_price and underlying_price > 0:
        if right == "C":
            # Keep strikes at or above current price (OTM calls)
            strikes = [s for s in strikes if s >= underlying_price * 0.85]
        else:
            strikes = [s for s in strikes if s <= underlying_price * 1.15]

    if not strikes:
        raise ValueError("Strike filter left no strikes to query.")

    logger.info(
        "Fetching %d %s option quotes for %s exp=%s …",
        len(strikes), "call" if right == "C" else "put", symbol, expiration
    )

    def fetch_one(strike: float) -> OptionQuote:
        contract = _make_option_contract(symbol, expiration, strike, right)
        data = client.request_option_snapshot(contract, timeout=timeout_per_strike)
        return OptionQuote(
            strike=strike,
            bid=data.get("bid", float("nan")),
            ask=data.get("ask", float("nan")),
            mid=data.get("mid", float("nan")),
        )

    quotes: list[OptionQuote] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_one, s): s for s in strikes}
        for fut in as_completed(futures):
            try:
                quotes.append(fut.result())
            except Exception as exc:
                logger.warning("Error fetching strike %s: %s", futures[fut], exc)

    quotes.sort(key=lambda q: q.strike)

    return pd.DataFrame(
        [
            {
                "strike": q.strike,
                "bid": q.bid,
                "ask": q.ask,
                "mid": q.mid,
                "spread": q.spread,
                "valid": q.is_valid,
            }
            for q in quotes
        ]
    )


def get_underlying_price(client: IBKRClient, symbol: str) -> float:
    """Fetch the last trade price of the underlying stock."""
    data = client.request_option_snapshot(_make_stk_contract(symbol), timeout=5.0)
    price = data.get("last", float("nan"))
    if math.isnan(price):
        price = data.get("mid", float("nan"))
    return price
