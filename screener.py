#!/usr/bin/env python3
"""
Options Ratio Screener – CLI entry point.

Usage examples
--------------
# Screen SOFI calls expiring 2024-03-15, show top 10 spreads
python screener.py SOFI 20240315

# Custom gap and lot size for commission display
python screener.py MARA 20240621 --gap 2 --lots 5 --top 15

# Use a specific Gateway port (live)
python screener.py HOOD 20240315 --port 4001
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

from ibkr_client import IBKRClient
from chain_fetcher import fetch_chain, get_underlying_price
from ratio_analyzer import find_ratio_spreads, RatioSpread

load_dotenv()

console = Console()
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _color_credit(net_credit: float) -> Text:
    s = f"${net_credit:+.2f}"
    if net_credit > 0:
        return Text(s, style="bold green")
    if net_credit == 0:
        return Text(s, style="cyan")
    return Text(s, style="red")


def _color_be_pct(pct: float | None) -> Text:
    if pct is None:
        return Text("∞", style="dim")
    s = f"+{pct:.1f}%"
    if pct >= 15:
        return Text(s, style="bold green")
    if pct >= 8:
        return Text(s, style="green")
    if pct >= 4:
        return Text(s, style="yellow")
    return Text(s, style="red")


def _flag(ok: bool) -> Text:
    return Text("✓", style="green") if ok else Text("~", style="yellow")


def print_spreads(
    spreads: list[RatioSpread],
    symbol: str,
    expiration: str,
    underlying_price: float,
    lots: int,
    top_n: int,
) -> None:
    if not spreads:
        console.print("[red]No credit ratio spreads found.[/red]")
        return

    exp_fmt = f"{expiration[:4]}-{expiration[4:6]}-{expiration[6:]}"
    up_str = f"${underlying_price:.2f}" if not math.isnan(underlying_price) else "N/A"

    console.print()
    console.rule(
        f"[bold cyan]Credit Ratio Spreads[/bold cyan]  "
        f"[white]{symbol}[/white]  exp=[yellow]{exp_fmt}[/yellow]  "
        f"spot=[green]{up_str}[/green]  lots=[white]{lots}[/white]"
    )
    console.print()

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white",
        show_lines=False,
    )

    table.add_column("#",           style="dim",    width=3,  justify="right")
    table.add_column("Long K",                               justify="right")
    table.add_column("Short K",                              justify="right")
    table.add_column("Ratio",                                justify="center")
    table.add_column("ATM?",                                 justify="center")
    table.add_column("Gross cr/sh",                          justify="right")
    table.add_column("Comm (1lot)",                          justify="right")
    table.add_column("Net cr (1lot)",                        justify="right")
    table.add_column(f"Net cr ({lots}lot)",                  justify="right")
    table.add_column("Upper BE",                             justify="right")
    table.add_column("BE% above spot",                       justify="right")
    table.add_column("Max profit",                           justify="right")

    for rank, s in enumerate(spreads[:top_n], start=1):
        net_n = s.net_credit_for_lots(lots)
        table.add_row(
            str(rank),
            f"{s.long_strike:.2f}",
            f"{s.short_strike:.2f}",
            f"1:{s.n_short}",
            _flag(s.long_in_atm_band),
            f"${s.gross_credit * 100:.2f}",
            f"${s.commission_1lot:.2f}",
            _color_credit(s.net_credit_1lot),
            _color_credit(net_n),
            f"{s.upper_BE:.2f}" if not math.isinf(s.upper_BE) else "∞",
            _color_be_pct(s.upper_BE_pct if not math.isinf(s.upper_BE_pct) else None),
            f"${s.max_profit_per_lot:.0f}",
        )

    console.print(table)
    console.print()
    console.print(
        "[dim]Score = upper_BE_pct (% above spot where you start losing). "
        "✓ = long within ATM+5%. ~ = long further OTM (relaxed band).[/dim]"
    )
    console.print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IBKR real-time credit ratio spread screener",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("symbol",     help="Underlying ticker (e.g. SOFI, MARA)")
    p.add_argument("expiration", help="Option expiration YYYYMMDD (e.g. 20240315)")
    p.add_argument("--host",      default=os.getenv("IBKR_HOST", "127.0.0.1"))
    p.add_argument("--port",      type=int, default=int(os.getenv("IBKR_PORT", 7497)))
    p.add_argument("--client-id", type=int, default=int(os.getenv("IBKR_CLIENT_ID", 1)))
    p.add_argument("--gap",       type=int, default=2,
                   help="Max strike steps between long and short leg (1-2 recommended)")
    p.add_argument("--lots",      type=int, default=1,
                   help="Reference lot size for commission display")
    p.add_argument("--top",       type=int, default=15, help="Results to display")
    p.add_argument("--strike-low",  type=float, default=None)
    p.add_argument("--strike-high", type=float, default=None)
    p.add_argument("--no-otm-filter", action="store_true",
                   help="Fetch all strikes (not just OTM)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    symbol     = args.symbol.upper()
    expiration = args.expiration.replace("-", "")

    if len(expiration) != 8 or not expiration.isdigit():
        console.print("[red]Expiration must be YYYYMMDD, e.g. 20240315[/red]")
        sys.exit(1)

    console.print(f"\n[cyan]Connecting to IBKR at {args.host}:{args.port} …[/cyan]")
    client = IBKRClient(host=args.host, port=args.port, client_id=args.client_id)
    try:
        client.connect_and_run()
    except ConnectionError as exc:
        console.print(f"[bold red]Connection failed:[/bold red] {exc}")
        sys.exit(1)

    try:
        console.print(f"[cyan]Fetching {symbol} price and option params …[/cyan]")
        with ThreadPoolExecutor(max_workers=2) as pool:
            price_fut  = pool.submit(get_underlying_price, client, symbol)
            params_fut = pool.submit(client.request_option_params, symbol)
            underlying_price = price_fut.result()
            all_strikes      = params_fut.result().get("strikes", [])

        if not math.isnan(underlying_price):
            console.print(f"  → spot: [green]${underlying_price:.2f}[/green]")
        else:
            console.print("  → [yellow]Could not get underlying price; OTM filter disabled.[/yellow]")

        if not all_strikes:
            console.print(f"[red]No option parameters for {symbol}.[/red]")
            sys.exit(1)

        strike_range: tuple[float, float] | None = None
        if args.strike_low or args.strike_high:
            strike_range = (args.strike_low or 0.0, args.strike_high or float("inf"))

        console.print(f"[cyan]Fetching call chain exp={expiration} …[/cyan]")
        t0 = time.time()
        chain_df = fetch_chain(
            client=client,
            symbol=symbol,
            expiration=expiration,
            right="C",
            strikes=all_strikes,
            timeout_per_strike=6.0,
            underlying_price=underlying_price,
            otm_only=not args.no_otm_filter,
            strike_range=strike_range,
        )
        elapsed = time.time() - t0
        valid_count = int(chain_df["valid"].sum())
        console.print(
            f"  → {len(chain_df)} strikes, [green]{valid_count} valid quotes[/green] "
            f"({elapsed:.1f}s)"
        )

        if valid_count == 0:
            console.print("[red]No valid quotes. Check market hours and data subscriptions.[/red]")
            sys.exit(1)

        console.print("[cyan]Scanning credit ratio spreads …[/cyan]")
        spreads = find_ratio_spreads(
            chain_df=chain_df,
            underlying_price=underlying_price,
            max_gap_steps=args.gap,
            top_n=args.top,
        )
        console.print(f"  → [green]{len(spreads)} candidates[/green]")

        print_spreads(
            spreads=spreads,
            symbol=symbol,
            expiration=expiration,
            underlying_price=underlying_price,
            lots=args.lots,
            top_n=args.top,
        )

    finally:
        client.disconnect_clean()


if __name__ == "__main__":
    main()
