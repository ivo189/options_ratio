"""
Bull-spread ratio analyzer.

A "ratio bullspread" here means:
  - BUY  1 call at strike K  (the *long leg*)
  - SELL N calls at strike K + gap_steps * step  (the *short leg*)
    where N ∈ {1, 2, 3, 4} and gap_steps ∈ {1, 2, 3, 4}

We score each combination by how favourable the entry price is:
  - For a 1:N spread the net debit (or credit) matters.
  - The *ratio* metric we use is:  long_ask / (short_bid * N)
    → values close to 1 mean the short premium almost fully funds the long
    → values < 1 mean the spread is entered for a NET CREDIT (ideal)
    → values > 1 mean you still pay a net debit, but less than buying the
      long outright.

We also compute:
  max_profit   = (short_strike - long_strike) * 100 * N  (for 1:N)
                 minus net_debit (or plus net_credit)     [per lot]
  breakeven    = long_strike + net_debit / 100            (for 1:1)
                 (For N>1 the breakeven is the long_strike + net_debit/100
                  on the upside; note that ratio spreads have upside risk
                  beyond the short strike, so we also report the upper BE.)
  score        = net_credit_or_minus_debit normalised by long_ask
                 Higher is better.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class SpreadCandidate:
    # Legs
    long_strike: float
    short_strike: float
    ratio: int          # number of short contracts per 1 long (1-4)
    gap_steps: int      # how many strike increments between legs

    # Prices (per contract, not per share; multiply by 100 for $ value)
    long_ask: float     # cost to buy the long leg
    short_bid: float    # credit per short contract

    # Derived
    net_debit: float = field(init=False)   # negative = credit
    funding_ratio: float = field(init=False)  # short_bid*ratio / long_ask
    score: float = field(init=False)
    lower_breakeven: float = field(init=False)
    upper_breakeven: float = field(init=False)  # only meaningful for ratio > 1
    max_profit_per_lot: float = field(init=False)  # $ (assuming 1 long, N short, 1 lot each)

    def __post_init__(self) -> None:
        short_credit = self.short_bid * self.ratio
        self.net_debit = self.long_ask - short_credit   # negative → credit

        if self.long_ask > 0:
            self.funding_ratio = short_credit / self.long_ask
        else:
            self.funding_ratio = float("nan")

        # Score: higher means cheaper entry relative to the spread width
        # We prefer: high funding_ratio AND wide spread width
        width = self.short_strike - self.long_strike
        if width > 0 and not math.isnan(self.net_debit):
            # Normalise debit to the spread width (lower debit-to-width → better)
            debit_to_width = self.net_debit / width  # can be negative (credit)
            self.score = -debit_to_width              # higher = better
        else:
            self.score = float("-inf")

        # Lower breakeven: long_strike + net_debit/1  (per share = debit/100 * 100)
        # net_debit is already per-share price (option price * 1 share notional)
        self.lower_breakeven = self.long_strike + self.net_debit

        # Upper breakeven (for ratio > 1): beyond short_strike the spread loses
        # For 1:N with N>1:
        #   profit at expiry S = max(S-long_strike,0)*100 - N*max(S-short_strike,0)*100 - net_debit*100
        #   at S > short_strike: profit = (S-long_strike) - N*(S-short_strike) - net_debit
        #                              = S(1-N) + N*short - long - net_debit
        #   Set = 0 → S = (long_strike + net_debit - N*short_strike) / (1 - N)
        if self.ratio > 1:
            denom = 1 - self.ratio
            self.upper_breakeven = (
                self.long_strike + self.net_debit - self.ratio * self.short_strike
            ) / denom
        else:
            self.upper_breakeven = float("inf")

        # Max profit at short_strike (for 1:N ratio spread)
        # profit at S = short_strike:
        #   = (short - long) - net_debit  [per share, so multiply by 100 for $]
        self.max_profit_per_lot = ((self.short_strike - self.long_strike) - self.net_debit) * 100

    @property
    def is_valid(self) -> bool:
        return (
            not math.isnan(self.long_ask)
            and not math.isnan(self.short_bid)
            and self.long_ask > 0
            and self.short_bid > 0
            and not math.isnan(self.score)
            and self.score > float("-inf")
        )

    def describe(self) -> dict:
        return {
            "long_strike": self.long_strike,
            "short_strike": self.short_strike,
            "ratio": f"1:{self.ratio}",
            "gap_steps": self.gap_steps,
            "long_ask": round(self.long_ask, 2),
            "short_bid": round(self.short_bid, 2),
            "short_credit_total": round(self.short_bid * self.ratio, 2),
            "net_debit": round(self.net_debit, 2),
            "funding_ratio_%": round(self.funding_ratio * 100, 1),
            "lower_BE": round(self.lower_breakeven, 2),
            "upper_BE": round(self.upper_breakeven, 2) if self.ratio > 1 else "∞",
            "max_profit_$": round(self.max_profit_per_lot, 2),
            "score": round(self.score, 4),
        }


def analyze_bull_spreads(
    chain_df: pd.DataFrame,
    max_gap_steps: int = 4,
    max_ratio: int = 4,
    min_funding_pct: float = 0.0,
    top_n: int = 20,
) -> list[SpreadCandidate]:
    """
    Given a chain DataFrame (columns: strike, bid, ask, mid, valid),
    generate all valid ratio bull-spread combinations and rank them.

    Parameters
    ----------
    chain_df        : output of chain_fetcher.fetch_chain
    max_gap_steps   : maximum number of strikes between long and short leg (1-4)
    max_ratio       : maximum short:long ratio (1-4)
    min_funding_pct : discard spreads where short premium funds less than this % of long
    top_n           : how many top candidates to return

    Returns
    -------
    List of SpreadCandidate sorted best-first by score.
    """
    valid = chain_df[chain_df["valid"]].copy()
    valid = valid.sort_values("strike").reset_index(drop=True)
    # After reset_index, the DataFrame index equals the positional row number (0, 1, 2, …)

    n = len(valid)
    candidates: list[SpreadCandidate] = []

    for i, long_row in valid.iterrows():
        long_ask = long_row["ask"]
        if long_ask <= 0 or math.isnan(long_ask):
            continue

        for gap in range(1, max_gap_steps + 1):
            short_idx = i + gap  # i is positional after reset_index
            if short_idx >= n:
                break

            short_row = valid.iloc[short_idx]
            short_bid = short_row["bid"]

            if short_bid <= 0 or math.isnan(short_bid):
                continue

            for ratio in range(1, max_ratio + 1):
                funding_pct = (short_bid * ratio) / long_ask * 100
                if funding_pct < min_funding_pct:
                    continue

                cand = SpreadCandidate(
                    long_strike=long_row["strike"],
                    short_strike=short_row["strike"],
                    ratio=ratio,
                    gap_steps=gap,
                    long_ask=long_ask,
                    short_bid=short_bid,
                )
                if cand.is_valid:
                    candidates.append(cand)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:top_n]
