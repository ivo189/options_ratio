"""Background scanner bot for ratio spread opportunities.

Scans a watchlist of symbols periodically, picks the best short-dated
expiration for each (target 5-21 DTE, prefer ~10-14 days), fetches the
call chain via IBKR, and surfaces the top credit ratio spreads.
"""

from __future__ import annotations

import logging
import math
import threading
import zoneinfo
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime
from typing import Callable

from chain_fetcher import fetch_chain, get_underlying_price
from ibkr_client import IBKRClient
from ratio_analyzer import find_ratio_spreads

logger = logging.getLogger(__name__)

_ET = zoneinfo.ZoneInfo("America/New_York")
_BOT_CLIENT_ID = 2  # distinct from the dashboard's client_id=1

_MARKET_OPEN  = dtime(9, 30)
_MARKET_CLOSE = dtime(16, 0)


def _market_is_open() -> bool:
    """Return True only during NYSE regular session (Mon-Fri 09:30-16:00 ET)."""
    now = datetime.now(_ET)
    return (
        now.weekday() < 5
        and _MARKET_OPEN <= now.time() < _MARKET_CLOSE
    )


# ---------------------------------------------------------------------------
# Expiration picker — short-dated (5-21 DTE, prefer ~10-14)
# ---------------------------------------------------------------------------

def pick_expiration(
    expirations: list[str],
    target_min_dte: int = 5,
    target_max_dte: int = 21,
    target_ideal_dte: int = 14,
) -> str | None:
    """
    Return the expiration (YYYYMMDD) closest to *target_ideal_dte* DTE,
    constrained to the [target_min_dte, target_max_dte] window.

    Falls back to the nearest future expiration if none lands in range.
    """
    today = date.today()
    scored: list[tuple[int, str]] = []

    for exp in expirations:
        try:
            exp_date = datetime.strptime(exp, "%Y%m%d").date()
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte <= 0:
            continue
        scored.append((dte, exp))

    if not scored:
        return None

    in_range = [x for x in scored if target_min_dte <= x[0] <= target_max_dte]
    pool = in_range if in_range else scored
    return min(pool, key=lambda x: abs(x[0] - target_ideal_dte))[1]


# ---------------------------------------------------------------------------
# Scanner bot
# ---------------------------------------------------------------------------

class ScannerBot:
    """Periodically scans a watchlist and caches ratio spread results."""

    def __init__(self, ibkr_host: str, ibkr_port: int) -> None:
        self.ibkr_host = ibkr_host
        self.ibkr_port = ibkr_port

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._symbols_getter: Callable[[], list[str]] = list

        self.interval_minutes: int = 5
        self.running: bool = False
        self.last_scan_et: str | None = None
        self.next_scan_et: str | None = None
        self.last_skipped_et: str | None = None   # last time a scan was skipped (market closed)

        # symbol -> result dict
        self.results: dict[str, dict] = {}
        self._results_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(
        self,
        interval_minutes: int = 5,
        symbols_getter: Callable[[], list[str]] | None = None,
    ) -> None:
        if self.running:
            self.interval_minutes = interval_minutes
            return
        self.interval_minutes = interval_minutes
        if symbols_getter is not None:
            self._symbols_getter = symbols_getter
        self._stop_event.clear()
        self.running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ScannerBot"
        )
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop_event.set()

    def scan_now(self, symbols: list[str] | None = None) -> None:
        """Trigger an immediate scan without waiting for the next interval."""
        syms = symbols if symbols is not None else self._symbols_getter()
        threading.Thread(
            target=self._do_scan, args=(syms,), daemon=True, name="ScanNow"
        ).start()

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "interval_minutes": self.interval_minutes,
            "market_open": _market_is_open(),
            "last_scan": self.last_scan_et,
            "last_skipped": self.last_skipped_et,
            "next_scan": self.next_scan_et,
        }

    def get_results(self) -> dict:
        with self._results_lock:
            return dict(self.results)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            if _market_is_open():
                symbols = self._symbols_getter()
                if symbols:
                    self._do_scan(symbols)
            else:
                self.last_skipped_et = datetime.now(_ET).strftime("%Y-%m-%d %H:%M ET")
                logger.debug("Market closed — skipping scan")
            self._stop_event.wait(timeout=self.interval_minutes * 60)
        self.running = False

    def _do_scan(self, symbols: list[str]) -> None:
        now_et = datetime.now(_ET)
        logger.info("Bot scan started for symbols: %s", symbols)

        try:
            client = IBKRClient(self.ibkr_host, self.ibkr_port, _BOT_CLIENT_ID)
            client.connect_and_run()
        except (ConnectionError, ValueError, OSError) as exc:
            logger.warning("Bot scan: IBKR connection failed: %s", exc)
            ts = now_et.strftime("%H:%M ET")
            with self._results_lock:
                for sym in symbols:
                    self.results[sym] = {
                        "symbol": sym,
                        "error": str(exc),
                        "scanned_at": ts,
                    }
            self._update_timestamps(now_et)
            return

        try:
            for sym in symbols:
                result = self._scan_symbol(client, sym)
                result["scanned_at"] = now_et.strftime("%H:%M ET")
                with self._results_lock:
                    self.results[sym] = result
        finally:
            try:
                client.disconnect_clean()
            except Exception:
                pass

        self._update_timestamps(now_et)
        logger.info("Bot scan complete")

    def _update_timestamps(self, now_et: datetime) -> None:
        self.last_scan_et = now_et.strftime("%Y-%m-%d %H:%M ET")
        self.next_scan_et = f"in {self.interval_minutes} min"

    def _scan_symbol(self, client: IBKRClient, symbol: str) -> dict:
        base = {"symbol": symbol, "candidates": [], "error": None}
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                price_fut  = pool.submit(get_underlying_price, client, symbol)
                params_fut = pool.submit(client.request_option_params, symbol)
                underlying_price = price_fut.result()
                params           = params_fut.result()

            expirations = params.get("expirations", [])
            all_strikes = params.get("strikes", [])

            if not expirations:
                return {**base, "error": "No expirations found"}

            expiration = pick_expiration(expirations)
            if not expiration:
                return {**base, "error": "No suitable expiration (need >0 DTE)"}

            dte = (datetime.strptime(expiration, "%Y%m%d").date() - date.today()).days

            chain_df = fetch_chain(
                client=client,
                symbol=symbol,
                expiration=expiration,
                right="C",
                strikes=all_strikes,
                underlying_price=underlying_price,
                otm_only=True,
            )

            valid_count = int(chain_df["valid"].sum())
            if valid_count == 0:
                return {
                    **base,
                    "expiration": expiration,
                    "dte": dte,
                    "error": "No valid quotes received",
                }

            spreads = find_ratio_spreads(
                chain_df=chain_df,
                underlying_price=underlying_price,
                top_n=5,
            )

            top = [s.describe(lots=1) for s in spreads]

            return {
                **base,
                "expiration":       expiration,
                "dte":              dte,
                "underlying_price": (
                    None if math.isnan(underlying_price)
                    else round(underlying_price, 2)
                ),
                "valid_quotes":     valid_count,
                "candidates":       top,
            }

        except Exception as exc:
            logger.warning("Bot scan error for %s: %s", symbol, exc)
            return {**base, "error": str(exc)}
