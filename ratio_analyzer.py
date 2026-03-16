"""
Ratio spread analyzer — aligned with the operational strategy:

  BUY  1 call at strike K  (the *long leg*)  →  ATM or slightly OTM
  SELL N calls at strike K + gap  (the *short leg*)  →  N ∈ {2, 3}, gap ∈ {1, 2}

Entry price convention (real executable prices):
  - Long leg:  paid at the ASK  (worst-case cost to buy)
  - Short leg: received at the BID  (worst-case credit when selling)
  - Mid-based credit is also computed for reference (optimistic fill).

Commission model: IBKR fixed pricing — $0.65/contract, minimum $1.00/order,
each leg treated as a separate order.

Score = upper_BE_pct (% above current price where the spread starts losing).
Higher score → more room before losing → higher probability of a positive close.

Primary sort: (is_credit, long_in_atm_band, score) — all descending.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd


# ---------------------------------------------------------------------------
# IBKR commission model
# ---------------------------------------------------------------------------

_COMMISSION_PER_CONTRACT = 0.65   # USD per contract
_COMMISSION_MIN_PER_ORDER = 1.00  # USD minimum per order (per leg)


def ibkr_commission(n_contracts: int) -> float:
    """IBKR fixed-price commission for one order of *n_contracts*."""
    return max(_COMMISSION_MIN_PER_ORDER, n_contracts * _COMMISSION_PER_CONTRACT)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class RatioSpread:
    """One ratio spread candidate (1 long : N short calls)."""

    # ── Market inputs ─────────────────────────────────────────────────────────
    underlying_price: float
    long_strike: float
    short_strike: float
    n_short: int            # contracts sold per 1 long  (2 or 3)

    # Entry prices (ask to buy, bid to sell — real executable prices)
    long_ask: float         # price paid to open the long leg
    short_bid: float        # credit received per short contract

    # Additional quotes for liquidity assessment and closing prices
    long_bid: float = float("nan")   # received if closing long early
    short_ask: float = float("nan")  # paid if closing short early

    # ── Derived (computed in __post_init__) ───────────────────────────────────
    # Entry credit (conservative: ask/bid)
    gross_credit: float = field(init=False)

    # Entry credit at mid prices (optimistic fill)
    mid_gross_credit: float = field(init=False)

    # Bid-ask spread as % of mid — liquidity indicator (lower = better)
    long_spread_pct: float = field(init=False)
    short_spread_pct: float = field(init=False)

    upper_BE: float = field(init=False)
    upper_BE_pct: float = field(init=False)
    max_profit_per_lot: float = field(init=False)

    commission_1lot: float = field(init=False)
    net_credit_1lot: float = field(init=False)

    long_in_atm_band: bool = field(init=False)
    is_credit: bool = field(init=False)
    score: float = field(init=False)

    def __post_init__(self) -> None:
        # Conservative entry credit (ask for long, bid for short)
        self.gross_credit = self.short_bid * self.n_short - self.long_ask
        self.is_credit = self.gross_credit > 0

        # Optimistic credit at mid prices
        long_mid  = (self.long_ask  + self.long_bid)  / 2 if not math.isnan(self.long_bid)  else self.long_ask
        short_mid = (self.short_bid + self.short_ask) / 2 if not math.isnan(self.short_ask) else self.short_bid
        self.mid_gross_credit = short_mid * self.n_short - long_mid

        # Bid-ask spread quality (per leg)
        def _spread_pct(bid: float, ask: float) -> float:
            if math.isnan(bid) or math.isnan(ask) or ask <= 0:
                return float("nan")
            mid = (bid + ask) / 2
            return (ask - bid) / mid * 100 if mid > 0 else float("nan")

        self.long_spread_pct  = _spread_pct(self.long_bid,  self.long_ask)
        self.short_spread_pct = _spread_pct(self.short_bid, self.short_ask)

        # Upper break-even: price above the short strike where P&L = 0
        #   Profit at S > short (per share) = (S-long) - n*(S-short) + gross_credit = 0
        #   → S = (n*short - long + gross_credit) / (n - 1)
        if self.n_short > 1:
            self.upper_BE = (
                self.n_short * self.short_strike
                - self.long_strike
                + self.gross_credit
            ) / (self.n_short - 1)
        else:
            self.upper_BE = float("inf")

        if self.underlying_price > 0 and not math.isinf(self.upper_BE):
            self.upper_BE_pct = (self.upper_BE / self.underlying_price - 1) * 100
        else:
            self.upper_BE_pct = float("inf")

        # Max profit at expiry when price pins at short_strike (per lot)
        self.max_profit_per_lot = (
            (self.short_strike - self.long_strike) + self.gross_credit
        ) * 100

        # IBKR commissions (two orders: buy long, sell n_short)
        self.commission_1lot = (
            ibkr_commission(1)
            + ibkr_commission(self.n_short)
        )

        self.net_credit_1lot = self.gross_credit * 100 - self.commission_1lot

        # ATM band check
        if self.underlying_price > 0:
            self.long_in_atm_band = self.long_strike <= self.underlying_price * 1.05
        else:
            self.long_in_atm_band = True

        self.score = self.upper_BE_pct if self.is_credit else float("-inf")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def net_credit_for_lots(self, n_lots: int) -> float:
        """Net dollar credit for *n_lots* of this spread (after IBKR commissions)."""
        gross_dollar = self.gross_credit * 100 * n_lots
        commission = ibkr_commission(n_lots) + ibkr_commission(n_lots * self.n_short)
        return gross_dollar - commission

    @property
    def is_valid(self) -> bool:
        return (
            not math.isnan(self.long_ask)
            and not math.isnan(self.short_bid)
            and self.long_ask > 0
            and self.short_bid > 0
        )

    def describe(self, lots: int = 1) -> dict:
        """Plain-dict summary suitable for JSON serialisation."""
        def _fmt(v: float, decimals: int = 4) -> float | None:
            return None if math.isnan(v) or math.isinf(v) else round(v, decimals)

        net_n = self.net_credit_for_lots(lots) if lots != 1 else None
        return {
            "long_strike":           self.long_strike,
            "short_strike":          self.short_strike,
            "ratio":                 f"1:{self.n_short}",
            "underlying_price":      round(self.underlying_price, 2),
            # ── Entry prices (real executable) ──
            "long_ask":              round(self.long_ask, 4),
            "short_bid":             round(self.short_bid, 4),
            # ── Closing prices (for roll / early exit) ──
            "long_bid":              _fmt(self.long_bid, 4),
            "short_ask":             _fmt(self.short_ask, 4),
            # ── Bid-ask spread quality ──
            "long_spread_pct":       _fmt(self.long_spread_pct, 1),
            "short_spread_pct":      _fmt(self.short_spread_pct, 1),
            # ── Credit scenarios ──
            "gross_credit":          round(self.gross_credit, 4),
            "gross_credit_$":        round(self.gross_credit * 100, 2),
            "mid_gross_credit_$":    round(self.mid_gross_credit * 100, 2),
            # ── Commissions ──
            "commission_1lot_$":     round(self.commission_1lot, 2),
            "net_credit_1lot_$":     round(self.net_credit_1lot, 2),
            **(
                {f"net_credit_{lots}lot_$": round(net_n, 2)}
                if net_n is not None else {}
            ),
            # ── Profit/loss landmarks ──
            "upper_BE":              _fmt(self.upper_BE, 2),
            "upper_BE_pct":          _fmt(self.upper_BE_pct, 1),
            "max_profit_1lot_$":     round(self.max_profit_per_lot, 2),
            # ── Flags ──
            "long_in_atm_band":      self.long_in_atm_band,
            "is_credit":             self.is_credit,
            "score":                 _fmt(self.score, 2),
        }


# ---------------------------------------------------------------------------
# Scanner function
# ---------------------------------------------------------------------------

def find_ratio_spreads(
    chain_df: pd.DataFrame,
    underlying_price: float,
    max_gap_steps: int = 2,
    n_short_options: tuple[int, ...] = (2, 3),
    atm_band_pct: float = 5.0,
    credit_only: bool = True,
    top_n: int = 20,
    lots: int = 1,
) -> list[RatioSpread]:
    """
    Scan *chain_df* for credit ratio spread opportunities.

    chain_df must have columns: strike, bid, ask, mid, valid.
    Prices are used as-is:
      - long leg  → paid at ASK
      - short leg → received at BID

    Returns a list of RatioSpread sorted best-first (see module docstring).
    """
    valid = (
        chain_df[chain_df["valid"]]
        .sort_values("strike")
        .reset_index(drop=True)
    )
    n_rows = len(valid)
    candidates: list[RatioSpread] = []

    for i in range(n_rows):
        long_row = valid.iloc[i]
        long_ask = float(long_row["ask"])
        long_bid = float(long_row["bid"]) if "bid" in long_row else float("nan")

        if long_ask <= 0 or math.isnan(long_ask):
            continue

        for gap in range(1, max_gap_steps + 1):
            j = i + gap
            if j >= n_rows:
                break

            short_row  = valid.iloc[j]
            short_bid  = float(short_row["bid"])
            short_ask  = float(short_row["ask"]) if "ask" in short_row else float("nan")

            if short_bid <= 0 or math.isnan(short_bid):
                continue

            for n_short in n_short_options:
                spread = RatioSpread(
                    underlying_price=underlying_price,
                    long_strike=float(long_row["strike"]),
                    short_strike=float(short_row["strike"]),
                    n_short=n_short,
                    long_ask=long_ask,
                    short_bid=short_bid,
                    long_bid=long_bid,
                    short_ask=short_ask,
                )
                if not spread.is_valid:
                    continue
                if credit_only and not spread.is_credit:
                    continue
                candidates.append(spread)

    candidates.sort(
        key=lambda s: (
            s.is_credit,
            s.long_in_atm_band,
            s.score if not math.isinf(s.score) else 0.0,
            s.gross_credit,
        ),
        reverse=True,
    )
    return candidates[:top_n]
