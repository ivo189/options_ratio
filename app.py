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
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify

from ibkr_client import IBKRClient
from chain_fetcher import fetch_chain, get_underlying_price
from ratio_analyzer import analyze_bull_spreads

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.WARNING)

IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", 7497))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", 1))


def _connect() -> IBKRClient:
    client = IBKRClient(host=IBKR_HOST, port=IBKR_PORT, client_id=IBKR_CLIENT_ID)
    client.connect_and_run()
    return client


@app.route("/")
def index():
    return render_template("index.html")


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

        # Format as YYYY-MM-DD for display
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
    """Run the ratio bull-spread screener and return results as JSON."""
    body = request.get_json(force=True)
    symbol = body.get("symbol", "").upper().strip()
    expiration = body.get("expiration", "").replace("-", "").strip()
    max_ratio = int(body.get("ratio", 4))
    max_gap = int(body.get("gap", 4))
    min_funding = float(body.get("min_funding", 0.0))
    top_n = int(body.get("top", 30))
    strike_low = body.get("strike_low")
    strike_high = body.get("strike_high")
    no_otm_filter = bool(body.get("no_otm_filter", False))

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    if not expiration or len(expiration) != 8 or not expiration.isdigit():
        return jsonify({"error": "expiration must be YYYYMMDD"}), 400
    if not (1 <= max_ratio <= 4):
        return jsonify({"error": "ratio must be 1-4"}), 400
    if not (1 <= max_gap <= 4):
        return jsonify({"error": "gap must be 1-4"}), 400

    strike_range = None
    if strike_low or strike_high:
        strike_range = (float(strike_low or 0), float(strike_high or 1e9))

    try:
        client = _connect()
        try:
            # Fetch underlying price and option params concurrently
            with ThreadPoolExecutor(max_workers=2) as pool:
                price_fut = pool.submit(get_underlying_price, client, symbol)
                params_fut = pool.submit(client.request_option_params, symbol)
                underlying_price = price_fut.result()
                all_strikes = params_fut.result().get("strikes", [])

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
                otm_only=not no_otm_filter,
                strike_range=strike_range,
            )
        finally:
            client.disconnect_clean()

        valid_count = int(chain_df["valid"].sum())
        if valid_count == 0:
            return jsonify({
                "error": "No valid quotes received. Check market hours and TWS subscriptions.",
                "strikes_fetched": len(chain_df),
                "valid_quotes": 0,
            }), 200

        candidates = analyze_bull_spreads(
            chain_df=chain_df,
            max_gap_steps=max_gap,
            max_ratio=max_ratio,
            min_funding_pct=min_funding,
            top_n=top_n,
        )

        results = []
        for rank, c in enumerate(candidates, start=1):
            d = c.describe()
            results.append({
                "rank": rank,
                "long_strike": c.long_strike,
                "short_strike": c.short_strike,
                "ratio": d["ratio"],
                "gap": c.gap_steps,
                "long_ask": d["long_ask"],
                "short_bid": d["short_bid"],
                "short_credit_total": d["short_credit_total"],
                "net_debit": d["net_debit"],
                "funding_pct": d["funding_ratio_%"],
                "lower_be": d["lower_BE"],
                "upper_be": str(d["upper_BE"]),
                "max_profit": d["max_profit_$"],
                "score": d["score"],
            })

        # Chain data for the chart (all valid strikes)
        chain_data = chain_df[chain_df["valid"]].assign(
            mid=lambda df: df["mid"].apply(lambda x: None if math.isnan(x) else round(x, 2))
        )[["strike", "bid", "ask", "mid"]].to_dict(orient="records")

        return jsonify({
            "symbol": symbol,
            "expiration": expiration,
            "underlying_price": None if math.isnan(underlying_price) else round(underlying_price, 2),
            "strikes_fetched": len(chain_df),
            "valid_quotes": valid_count,
            "candidates": results,
            "chain": chain_data,
        })

    except ConnectionError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        logging.exception("Screener error")
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", 5000))
    print(f"\n  Options Ratio Screener dashboard → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
