"""
Ratio spread analyzer — aligned with the operational strategy:

  BUY  1 call at strike K  (the *long leg*)  →  ATM or slightly OTM
  SELL N calls at strike K + gap  (the *short leg*)  →  N ∈ {2, 3}, gap ∈ {1, 2}

Key constraints and design decisions:
  - Only NET CREDIT entries are shown (gross_credit > 0 before commissions).
  - The long leg should be within ATM+5 % of the underlying price; candidates
    outside that band are still returned but flagged (long_in_atm_band=False).
  - Commission model: IBKR fixed pricing — $0.65/contract, minimum $1.00/order,
    with each leg treated as a separate order.
  - Score = upper_BE_pct (how far above current price the break-even is).
    Higher score → more room for the underlying to move before you lose money
    → higher probability of a positive close.

Primary sort key: (is_credit, long_in_atm_band, score) — all descending.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd


# ---------------------------------------------------------------------------
# IBKR commission model
# ---------------------------------------------------------------------------

_COMMISSION_PER_CONTRACT = 0.65   # USD
_COMMISSION_MIN_PER_ORDER = 1.00  # USD minimum per order (leg)


def ibkr_commission(n_contracts: int) -> float:
    """Return IBKR fixed-price commission for one order of *n_contracts*."""
    return max(_COMMISSION_MIN_PER_ORDER, n_contracts * _COMMISSION_PER_CONTRACT)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class RatioSpread:
    """One ratio spread candidate (1 long : N short calls)."""

    # ── Inputs ────────────────────────────────────────────────────────────────
    underlying_price: float
    long_strike: float
    short_strike: float
    n_short: int            # contracts sold per 1 long  (2 or 3)

    long_ask: float         # cost to buy the long leg (price per share)
    short_bid: float        # credit per short contract (price per share)

    # ── Derived (computed in __post_init__) ───────────────────────────────────
    gross_credit: float = field(init=False)         # per share, positive = credit
    upper_BE: float = field(init=False)             # upper break-even price
    upper_BE_pct: float = field(init=False)         # % above underlying_price
    max_profit_per_lot: float = field(init=False)   # $ at expiry if price = short_strike

    commission_1lot: float = field(init=False)      # IBKR commission for 1 lot
    net_credit_1lot: float = field(init=False)      # dollar net credit for 1 lot

    long_in_atm_band: bool = field(init=False)      # long_strike ≤ underlying * 1.05
    is_credit: bool = field(init=False)             # gross_credit > 0
    score: float = field(init=False)                # primary ranking metric

    def __post_init__(self) -> None:
        # Gross credit per share (positive → we receive money)
        self.gross_credit = self.short_bid * self.n_short - self.long_ask
        self.is_credit = self.gross_credit > 0

        # Upper break-even: the price above the short strike where P&L = 0
        #   Profit at S > short_strike (per share) =
        #     (S - long) - n*(S - short) + gross_credit
        #   = S(1-n) + n*short - long + gross_credit = 0
        #   → S = (n*short - long + gross_credit) / (n - 1)
        if self.n_short > 1:
            self.upper_BE = (
                self.n_short * self.short_strike
                - self.long_strike
                + self.gross_credit
            ) / (self.n_short - 1)
        else:
            # 1:1 ratio has no upper break-even (bounded loss)
            self.upper_BE = float("inf")

        if self.underlying_price > 0 and not math.isinf(self.upper_BE):
            self.upper_BE_pct = (self.upper_BE / self.underlying_price - 1) * 100
        else:
            self.upper_BE_pct = float("inf")

        # Max profit at expiry when price = short_strike (per lot = 100 shares)
        self.max_profit_per_lot = (
            (self.short_strike - self.long_strike) + self.gross_credit
        ) * 100

        # IBKR commissions (two orders: one to buy the long, one to sell the shorts)
        self.commission_1lot = (
            ibkr_commission(1)               # buy 1 long
            + ibkr_commission(self.n_short)  # sell n_short contracts
        )

        # Net credit in dollars for 1 lot
        self.net_credit_1lot = self.gross_credit * 100 - self.commission_1lot

        # ATM band: long strike within 5 % above current price
        if self.underlying_price > 0:
            self.long_in_atm_band = self.long_strike <= self.underlying_price * 1.05
        else:
            self.long_in_atm_band = True   # unknown → assume ok

        # Score: upper_BE_pct (only meaningful for credit spreads)
        self.score = self.upper_BE_pct if self.is_credit else float("-inf")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def net_credit_for_lots(self, n_lots: int) -> float:
        """Net dollar credit for *n_lots* of this spread (after IBKR commissions)."""
        gross_dollar = self.gross_credit * 100 * n_lots
        commission = (
            ibkr_commission(n_lots)
            + ibkr_commission(n_lots * self.n_short)
        )
        return gross_dollar - commission

    @property
    def is_valid(self) -> bool:
        return (
            not math.isnan(self.long_ask)
            and not math.isnan(self.short_bid)
            and self.long_ask > 0
            and self.short_bid > 0
            and not math.isnan(self.gross_credit)
        )

    def describe(self, lots: int = 1) -> dict:
        """Return a plain-dict summary suitable for JSON serialisation."""
        net_credit_nlot = self.net_credit_for_lots(lots) if lots != 1 else None
        return {
            "long_strike":        self.long_strike,
            "short_strike":       self.short_strike,
            "ratio":              f"1:{self.n_short}",
            "underlying_price":   round(self.underlying_price, 2),
            "long_ask":           round(self.long_ask, 4),
            "short_bid":          round(self.short_bid, 4),
            "gross_credit":       round(self.gross_credit, 4),
            "gross_credit_$":     round(self.gross_credit * 100, 2),
            "commission_1lot_$":  round(self.commission_1lot, 2),
            "net_credit_1lot_$":  round(self.net_credit_1lot, 2),
            **(
                {f"net_credit_{lots}lot_$": round(net_credit_nlot, 2)}
                if net_credit_nlot is not None else {}
            ),
            "upper_BE":           round(self.upper_BE, 2) if not math.isinf(self.upper_BE) else None,
            "upper_BE_pct":       round(self.upper_BE_pct, 1) if not math.isinf(self.upper_BE_pct) else None,
            "max_profit_1lot_$":  round(self.max_profit_per_lot, 2),
            "long_in_atm_band":   self.long_in_atm_band,
            "is_credit":          self.is_credit,
            "score":              round(self.score, 2) if not math.isinf(self.score) else None,
        }


# ---------------------------------------------------------------------------
# Scanner function
# ---------------------------------------------------------------------------

def find_ratio_spreads(
    chain_df: pd.DataFrame,
    underlying_price: float,
    max_gap_steps: int = 2,         # +1 or +2 strikes between long and short
    n_short_options: tuple[int, ...] = (2, 3),   # only 1:2 and 1:3
    atm_band_pct: float = 5.0,      # soft upper limit for long leg (% above spot)
    credit_only: bool = True,        # discard net-debit combinations
    top_n: int = 20,
    lots: int = 1,                   # reference lot size for commission display
) -> list[RatioSpread]:
    """
    Scan *chain_df* (output of chain_fetcher.fetch_chain) for ratio spread
    opportunities that match the operational strategy.

    Ranking (all descending):
      1. is_credit=True first
      2. long_in_atm_band=True first
      3. score (= upper_BE_pct) highest first
      4. gross_credit highest first (tiebreaker)

    Parameters
    ----------
    chain_df        : DataFrame with columns strike, bid, ask, mid, valid
    underlying_price: current price of the underlying stock
    max_gap_steps   : maximum number of strike increments between long and short
    n_short_options : which short ratios to consider
    atm_band_pct    : long strike must be ≤ underlying * (1 + atm_band_pct/100)
                      to be flagged as long_in_atm_band=True
    credit_only     : if True, only return net-credit spreads
    top_n           : maximum number of results
    lots            : display net credit for this many lots (in describe())

    Returns
    -------
    List of RatioSpread sorted best-first.
    """
    valid = chain_df[chain_df["valid"]].copy()
    valid = valid.sort_values("strike").reset_index(drop=True)

    n_rows = len(valid)
    candidates: list[RatioSpread] = []

    for i, long_row in valid.iterrows():
        long_ask = long_row["ask"]
        if long_ask <= 0 or math.isnan(long_ask):
            continue

        for gap in range(1, max_gap_steps + 1):
            short_idx = i + gap
            if short_idx >= n_rows:
                break

            short_row = valid.iloc[short_idx]
            short_bid = short_row["bid"]
            if short_bid <= 0 or math.isnan(short_bid):
                continue

            for n_short in n_short_options:
                spread = RatioSpread(
                    underlying_price=underlying_price,
                    long_strike=long_row["strike"],
                    short_strike=short_row["strike"],
                    n_short=n_short,
                    long_ask=long_ask,
                    short_bid=short_bid,
                )
                if not spread.is_valid:
                    continue
                if credit_only and not spread.is_credit:
                    continue
                candidates.append(spread)

    # Sort: credit first, ATM-band first, then by score (upper_BE_pct), then credit size
    candidates.sort(
        key=lambda s: (
            s.is_credit,
            s.long_in_atm_band,
            s.score if not math.isinf(s.score) else 0,
            s.gross_credit,
        ),
        reverse=True,
    )
    return candidates[:top_n]
