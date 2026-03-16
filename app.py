#!/usr/bin/env python3
"""
Flask web server for the Options Ratio Screener dashboard.

Run with:
    python app.py
Then open http://localhost:5000 in your browser.
"""

from __future__ import annotations

import math
import logging
import os
import zoneinfo
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, date

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify

from ibkr_client import IBKRClient
from chain_fetcher import fetch_chain, get_underlying_price
from ratio_analyzer import analyze_bull_spreads
import watchlist as wl
from scanner_bot import ScannerBot

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.WARNING)

IBKR_HOST      = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT      = int(os.getenv("IBKR_PORT", 7497))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", 1))

_ET = zoneinfo.ZoneInfo("America/New_York")

# NYSE regular-session open/close times (Eastern)
_MARKET_OPEN  = dtime(9, 30)
_MARKET_CLOSE = dtime(16, 0)

# In-memory cache: (symbol, expiration) -> last successful screen payload
_result_cache: dict[tuple[str, str], dict] = {}

# Scanner bot (single global instance)
_bot = ScannerBot(ibkr_host=IBKR_HOST, ibkr_port=IBKR_PORT)


# ---------------------------------------------------------------------------
# Market helpers
# ---------------------------------------------------------------------------

def _market_status() -> dict:
    """Return current NYSE market status and ET time."""
    now = datetime.now(_ET)
    t   = now.time()
    # Weekday: 0=Mon … 4=Fri, 5=Sat, 6=Sun
    is_weekday    = now.weekday() < 5
    in_session    = is_weekday and _MARKET_OPEN <= t < _MARKET_CLOSE
    pre_market    = is_weekday and dtime(4, 0) <= t < _MARKET_OPEN
    after_hours   = is_weekday and _MARKET_CLOSE <= t < dtime(20, 0)

    if in_session:
        session = "open"
        # seconds until close
        close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
        secs_left = int((close_dt - now).total_seconds())
    elif pre_market:
        session = "pre-market"
        secs_left = None
    elif after_hours:
        session = "after-hours"
        secs_left = None
    else:
        session = "closed"
        secs_left = None

    return {
        "session":   session,
        "open":      in_session,
        "time_et":   now.strftime("%H:%M:%S"),
        "date_et":   now.strftime("%Y-%m-%d"),
        "weekday":   now.strftime("%A"),
        "secs_to_close": secs_left,
    }


# ---------------------------------------------------------------------------
# IBKR helpers
# ---------------------------------------------------------------------------

def _connect() -> IBKRClient:
    client = IBKRClient(host=IBKR_HOST, port=IBKR_PORT, client_id=IBKR_CLIENT_ID)
    client.connect_and_run()
    return client


def _build_response(
    symbol: str,
    expiration: str,
    underlying_price: float,
    chain_df,
    candidates,
) -> dict:
    """Assemble the JSON payload returned by /api/screen."""
    results = []
    for rank, c in enumerate(candidates, start=1):
        d = c.describe()
        results.append({
            "rank":               rank,
            "long_strike":        c.long_strike,
            "short_strike":       c.short_strike,
            "ratio":              d["ratio"],
            "gap":                c.gap_steps,
            "long_ask":           d["long_ask"],
            "short_bid":          d["short_bid"],
            "short_credit_total": d["short_credit_total"],
            "net_debit":          d["net_debit"],
            "funding_pct":        d["funding_ratio_%"],
            "lower_be":           d["lower_BE"],
            "upper_be":           str(d["upper_BE"]),
            "max_profit":         d["max_profit_$"],
            "score":              d["score"],
        })

    chain_data = (
        chain_df[chain_df["valid"]]
        .assign(mid=lambda df: df["mid"].apply(
            lambda x: None if math.isnan(x) else round(x, 2)
        ))[["strike", "bid", "ask", "mid"]]
        .to_dict(orient="records")
    )

    status = _market_status()
    now_et = datetime.now(_ET)

    return {
        "symbol":           symbol,
        "expiration":       expiration,
        "underlying_price": None if math.isnan(underlying_price) else round(underlying_price, 2),
        "strikes_fetched":  len(chain_df),
        "valid_quotes":     int(chain_df["valid"].sum()),
        "candidates":       results,
        "chain":            chain_data,
        "market":           status,
        "fetched_at":       now_et.strftime("%Y-%m-%d %H:%M:%S ET"),
        "fetched_at_ts":    int(now_et.timestamp()),
        "from_cache":       False,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/market-status")
def api_market_status():
    return jsonify(_market_status())


@app.route("/api/expirations")
def api_expirations():
    """Return available option expirations for a symbol."""
    symbol = request.args.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    try:
        client = _connect()
        try:
            params = client.request_option_params(symbol)
        finally:
            client.disconnect_clean()

        expirations = params.get("expirations", [])
        if not expirations:
            return jsonify({"error": f"No option data found for {symbol}"}), 404

        formatted = [
            {"value": e, "label": f"{e[:4]}-{e[4:6]}-{e[6:]}"}
            for e in expirations
        ]
        return jsonify({"expirations": formatted})

    except ConnectionError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/screen", methods=["POST"])
def api_screen():
    """
    Run the ratio bull-spread screener and return results as JSON.

    When the market is closed and a previous result is cached for the same
    symbol+expiration, the cached payload is returned immediately (no IBKR
    request).  The response includes `from_cache: true` and the original
    `fetched_at` timestamp so the frontend can warn the user.

    When the market is open (or there is no cache), a fresh IBKR request is
    made and the result replaces the cache entry.
    """
    body        = request.get_json(force=True)
    symbol      = body.get("symbol", "").upper().strip()
    expiration  = body.get("expiration", "").replace("-", "").strip()
    max_ratio   = int(body.get("ratio", 4))
    max_gap     = int(body.get("gap", 4))
    min_funding = float(body.get("min_funding", 0.0))
    top_n       = int(body.get("top", 30))
    strike_low  = body.get("strike_low")
    strike_high = body.get("strike_high")
    no_otm      = bool(body.get("no_otm_filter", False))
    force_fresh = bool(body.get("force_fresh", False))

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    if not expiration or len(expiration) != 8 or not expiration.isdigit():
        return jsonify({"error": "expiration must be YYYYMMDD"}), 400
    if not (1 <= max_ratio <= 4):
        return jsonify({"error": "ratio must be 1-4"}), 400
    if not (1 <= max_gap <= 4):
        return jsonify({"error": "gap must be 1-4"}), 400

    cache_key  = (symbol, expiration)
    status     = _market_status()
    market_open = status["open"]

    # ── Serve cache when market is closed and we have previous data ──────────
    if not market_open and not force_fresh and cache_key in _result_cache:
        cached = dict(_result_cache[cache_key])
        cached["from_cache"] = True
        cached["market"]     = status   # always refresh market status block
        return jsonify(cached)

    # ── Fetch from IBKR ──────────────────────────────────────────────────────
    strike_range = None
    if strike_low or strike_high:
        strike_range = (float(strike_low or 0), float(strike_high or 1e9))

    try:
        client = _connect()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                price_fut  = pool.submit(get_underlying_price, client, symbol)
                params_fut = pool.submit(client.request_option_params, symbol)
                underlying_price = price_fut.result()
                all_strikes      = params_fut.result().get("strikes", [])

            if not all_strikes:
                return jsonify({"error": f"No option parameters for {symbol}"}), 404

            chain_df = fetch_chain(
                client=client,
                symbol=symbol,
                expiration=expiration,
                right="C",
                strikes=all_strikes,
                timeout_per_strike=6.0,
                underlying_price=underlying_price,
                otm_only=not no_otm,
                strike_range=strike_range,
            )
        finally:
            client.disconnect_clean()

        valid_count = int(chain_df["valid"].sum())
        if valid_count == 0:
            # If we have a cache entry, return it with a warning
            if cache_key in _result_cache:
                cached = dict(_result_cache[cache_key])
                cached["from_cache"] = True
                cached["market"]     = status
                cached["warning"]    = "No live quotes received — showing last known data."
                return jsonify(cached)
            return jsonify({
                "error": "No valid quotes received. Check market hours and TWS subscriptions.",
            }), 200

        candidates = analyze_bull_spreads(
            chain_df=chain_df,
            max_gap_steps=max_gap,
            max_ratio=max_ratio,
            min_funding_pct=min_funding,
            top_n=top_n,
        )

        payload = _build_response(symbol, expiration, underlying_price, chain_df, candidates)

        # Store in cache (overwrite any previous entry)
        _result_cache[cache_key] = payload

        return jsonify(payload)

    except ConnectionError as exc:
        # IBKR unreachable — serve cache if available
        if cache_key in _result_cache:
            cached = dict(_result_cache[cache_key])
            cached["from_cache"] = True
            cached["market"]     = status
            cached["warning"]    = f"IBKR unreachable ({exc}) — showing last known data."
            return jsonify(cached)
        return jsonify({"error": str(exc)}), 503

    except Exception as exc:
        logging.exception("Screener error")
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Watchlist routes
# ---------------------------------------------------------------------------

@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    return jsonify({"symbols": wl.get_symbols()})


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    body   = request.get_json(force=True)
    symbol = body.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    return jsonify({"symbols": wl.add_symbol(symbol)})


@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
def api_watchlist_remove(symbol: str):
    return jsonify({"symbols": wl.remove_symbol(symbol.upper())})


# ---------------------------------------------------------------------------
# Bot routes
# ---------------------------------------------------------------------------

@app.route("/api/bot/status")
def api_bot_status():
    return jsonify(_bot.get_status())


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    body     = request.get_json(force=True) or {}
    interval = int(body.get("interval_minutes", 15))
    if not (1 <= interval <= 120):
        return jsonify({"error": "interval_minutes must be 1-120"}), 400
    _bot.start(interval_minutes=interval, symbols_getter=wl.get_symbols)
    return jsonify(_bot.get_status())


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    _bot.stop()
    return jsonify(_bot.get_status())


@app.route("/api/bot/scan-now", methods=["POST"])
def api_bot_scan_now():
    symbols = wl.get_symbols()
    if not symbols:
        return jsonify({"error": "Watchlist is empty"}), 400
    _bot.scan_now(symbols)
    return jsonify({"ok": True, "scanning": symbols})


@app.route("/api/bot/results")
def api_bot_results():
    return jsonify(_bot.get_results())


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", 5000))
    print(f"\n  Options Ratio Screener dashboard → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
