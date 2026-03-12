#!/usr/bin/env python3
"""
Options Ratio Screener – main entry point.

Usage examples
--------------
# Screen SPY calls expiring 2024-03-15, show top 15 spreads
python screener.py SPY 20240315

# Screen with custom filters
python screener.py AAPL 20240621 --ratio 4 --gap 3 --min-funding 50 --top 20

# Use a specific TWS port (live)
python screener.py QQQ 20240315 --port 7496
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

from ibkr_client import IBKRClient
from chain_fetcher import fetch_chain, get_underlying_price
from ratio_analyzer import analyze_bull_spreads, SpreadCandidate

load_dotenv()

console = Console()
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _color_funding(pct: float) -> Text:
    s = f"{pct:.1f}%"
    if pct >= 100:
        return Text(s, style="bold green")
    if pct >= 75:
        return Text(s, style="green")
    if pct >= 50:
        return Text(s, style="yellow")
    return Text(s, style="red")


def _color_debit(debit: float) -> Text:
    s = f"{'−' if debit < 0 else '+'}{abs(debit):.2f}"
    if debit < 0:
        return Text(s, style="bold green")   # net credit
    if debit == 0:
        return Text("0.00", style="cyan")
    return Text(s, style="yellow")


def _color_score(score: float) -> Text:
    s = f"{score:.4f}"
    if score > 0.3:
        return Text(s, style="bold green")
    if score > 0:
        return Text(s, style="green")
    if score > -0.1:
        return Text(s, style="yellow")
    return Text(s, style="red")


def print_candidates(
    candidates: list[SpreadCandidate],
    symbol: str,
    expiration: str,
    underlying_price: float,
    top_n: int,
) -> None:
    if not candidates:
        console.print("[red]No valid spread candidates found.[/red]")
        return

    exp_fmt = f"{expiration[:4]}-{expiration[4:6]}-{expiration[6:]}"
    up_str = f"${underlying_price:.2f}" if not math.isnan(underlying_price) else "N/A"

    console.print()
    console.rule(
        f"[bold cyan]Ratio Bull-Spread Screener[/bold cyan]  "
        f"[white]{symbol}[/white]  exp=[yellow]{exp_fmt}[/yellow]  "
        f"underlying≈[green]{up_str}[/green]"
    )
    console.print()

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white",
        show_lines=False,
    )

    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("Long K", justify="right")
    table.add_column("Short K", justify="right")
    table.add_column("Ratio", justify="center")
    table.add_column("Gap", justify="center")
    table.add_column("Long ask", justify="right")
    table.add_column("Short bid", justify="right")
    table.add_column("Net $/share", justify="right")
    table.add_column("Funded %", justify="right")
    table.add_column("Lower BE", justify="right")
    table.add_column("Upper BE", justify="right")
    table.add_column("Max profit $", justify="right")
    table.add_column("Score", justify="right")

    for rank, c in enumerate(candidates[:top_n], start=1):
        d = c.describe()
        upper_be = f"{d['upper_BE']}" if c.ratio == 1 else f"{d['upper_BE']}"
        table.add_row(
            str(rank),
            f"{c.long_strike:.2f}",
            f"{c.short_strike:.2f}",
            d["ratio"],
            str(c.gap_steps),
            f"{c.long_ask:.2f}",
            f"{c.short_bid:.2f}",
            _color_debit(c.net_debit),
            _color_funding(c.funding_ratio * 100),
            f"{d['lower_BE']:.2f}",
            str(upper_be),
            f"{d['max_profit_$']:.0f}",
            _color_score(c.score),
        )

    console.print(table)
    console.print()
    console.print(
        "[dim]Score = −(net_debit / spread_width). Higher is better. "
        "Negative net = credit. "
        "Upper BE only meaningful for ratio > 1:1.[/dim]"
    )
    console.print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IBKR real-time ratio bull-spread screener",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("symbol", help="Underlying ticker symbol (e.g. SPY, AAPL)")
    p.add_argument("expiration", help="Option expiration in YYYYMMDD format (e.g. 20240315)")
    p.add_argument("--host", default=os.getenv("IBKR_HOST", "127.0.0.1"), help="TWS/Gateway host")
    p.add_argument("--port", type=int, default=int(os.getenv("IBKR_PORT", 7497)), help="TWS/Gateway port")
    p.add_argument("--client-id", type=int, default=int(os.getenv("IBKR_CLIENT_ID", 1)), help="API client ID")
    p.add_argument("--ratio", type=int, default=4, help="Max short:long ratio to consider (1-4)")
    p.add_argument("--gap", type=int, default=4, help="Max strike gap steps between legs (1-4)")
    p.add_argument("--min-funding", type=float, default=0.0,
                   help="Minimum funding %% (short premium / long ask * 100). E.g. 50 = short covers ≥50%% of long.")
    p.add_argument("--top", type=int, default=20, help="Number of top candidates to display")
    p.add_argument("--strike-low", type=float, default=None,
                   help="Lower bound for strikes to include (absolute price)")
    p.add_argument("--strike-high", type=float, default=None,
                   help="Upper bound for strikes to include (absolute price)")
    p.add_argument("--no-otm-filter", action="store_true",
                   help="Disable the automatic OTM-only filter (fetch all strikes)")
    p.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    symbol = args.symbol.upper()
    expiration = args.expiration.replace("-", "")

    if len(expiration) != 8 or not expiration.isdigit():
        console.print("[red]Expiration must be YYYYMMDD, e.g. 20240315[/red]")
        sys.exit(1)

    console.print(f"\n[cyan]Connecting to IBKR at {args.host}:{args.port} (client_id={args.client_id}) …[/cyan]")

    client = IBKRClient(host=args.host, port=args.port, client_id=args.client_id)
    try:
        client.connect_and_run()
    except ConnectionError as exc:
        console.print(f"[bold red]Connection failed:[/bold red] {exc}")
        sys.exit(1)

    try:
        # 1. Get underlying price for OTM filtering
        console.print(f"[cyan]Fetching {symbol} underlying price …[/cyan]")
        underlying_price = get_underlying_price(client, symbol)
        if not math.isnan(underlying_price):
            console.print(f"  → Underlying last/mid: [green]${underlying_price:.2f}[/green]")
        else:
            console.print("  → [yellow]Could not determine underlying price; OTM filter disabled.[/yellow]")

        # 2. Fetch the options chain
        strike_range: Optional[tuple[float, float]] = None
        if args.strike_low or args.strike_high:
            lo = args.strike_low or 0.0
            hi = args.strike_high or float("inf")
            strike_range = (lo, hi)

        console.print(f"[cyan]Fetching call chain for {symbol} exp={expiration} …[/cyan]")
        t0 = time.time()
        chain_df = fetch_chain(
            client=client,
            symbol=symbol,
            expiration=expiration,
            right="C",
            timeout_per_strike=6.0,
            underlying_price=underlying_price,
            otm_only=not args.no_otm_filter,
            strike_range=strike_range,
        )
        elapsed = time.time() - t0
        valid_count = chain_df["valid"].sum()
        console.print(
            f"  → {len(chain_df)} strikes fetched, [green]{valid_count} valid quotes[/green] "
            f"({elapsed:.1f}s)"
        )

        if valid_count == 0:
            console.print("[red]No valid quotes received. Check market hours and TWS market data subscriptions.[/red]")
            sys.exit(1)

        # 3. Analyze spreads
        console.print("[cyan]Analyzing ratio bull-spread combinations …[/cyan]")
        candidates = analyze_bull_spreads(
            chain_df=chain_df,
            max_gap_steps=min(args.gap, 4),
            max_ratio=min(args.ratio, 4),
            min_funding_pct=args.min_funding,
            top_n=args.top,
        )
        console.print(f"  → [green]{len(candidates)} candidates[/green] found")

        # 4. Display results
        print_candidates(
            candidates=candidates,
            symbol=symbol,
            expiration=expiration,
            underlying_price=underlying_price,
            top_n=args.top,
        )

    finally:
        client.disconnect_clean()


if __name__ == "__main__":
    main()
