"""
Roll advisor for ratio call spreads.

When the underlying moves up toward or past the break-even, the position
can be "pyramided" (rolled up) to a higher set of strikes while staying
within the same expiration.  The mechanics for a 1:M ratio with N lots:

  Step 1 — Close original long:
    Sell N contracts at original_long_strike  (at BID)

  Step 2 — Transform original shorts into new longs:
    Buy (M + 2) * N contracts at original_short_strike  (at ASK)
    → closes M*N existing shorts + opens 2*N new longs at that strike

  Step 3 — Open new short leg:
    Sell 2*M*N contracts at new_short_strike  (at BID)
    → maintains the 1:M ratio  (2*N long : 2*M*N short)

After the roll the position becomes:
    Long  2*N  calls at original_short_strike
    Short 2*M*N calls at new_short_strike

Accumulated credit = original entry net credit + roll net credit (both
after IBKR commissions).  The new upper break-even is computed from the
accumulated credit so the user sees whether the entire combined position
remains profitable.

All dollar amounts are in USD.  "Per share" means per underlying share
(option prices before the ×100 multiplier).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ratio_analyzer import ibkr_commission


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class RollScenario:
    """One possible roll — a specific choice of new_short_strike."""

    # ── Inputs: original position ─────────────────────────────────────────────
    original_long_strike: float
    original_short_strike: float
    n_short: int                    # M — shorts per long in the original ratio (2 or 3)
    lots: int                       # N — number of lots (long contracts) in original position
    entry_net_credit: float         # $ collected when the position was opened (after commissions)

    # ── Inputs: current market prices ────────────────────────────────────────
    underlying_price: float
    long_bid_current: float         # bid of original long (received in step 1)
    short_ask_current: float        # ask of original short (paid in step 2)
    new_short_strike: float
    new_short_bid: float            # bid of new short strike (received in step 3)

    # ── Derived ───────────────────────────────────────────────────────────────
    # Roll transaction counts
    n_close_long: int = field(init=False)         # step 1: contracts sold
    n_transform: int = field(init=False)          # step 2: contracts bought
    n_new_short: int = field(init=False)          # step 3: contracts sold

    # Roll P&L (before and after commissions)
    roll_gross_credit: float = field(init=False)  # $ gross credit from roll transaction
    roll_commission: float = field(init=False)    # $ IBKR commissions for roll
    roll_net_credit: float = field(init=False)    # $ net credit from roll (can be negative)

    # Accumulated position after the roll
    accumulated_net_credit: float = field(init=False)  # $ total since inception
    new_lots: int = field(init=False)             # long contracts in new position (= 2*N)

    # New position landmarks (based on accumulated credit)
    new_upper_BE: float = field(init=False)
    new_upper_BE_pct: float = field(init=False)
    new_max_profit_per_lot: float = field(init=False)

    # Viability flags
    roll_is_credit: bool = field(init=False)          # roll itself produces a credit
    position_is_profitable_floor: bool = field(init=False)  # accumulated credit > 0

    def __post_init__(self) -> None:
        M, N = self.n_short, self.lots

        # Step counts
        self.n_close_long  = N           # sell N longs
        self.n_transform   = (M + 2) * N  # buy to close M*N + go long 2*N
        self.n_new_short   = 2 * M * N  # sell to maintain 1:M ratio

        # Roll gross credit (in dollars, using real bid/ask)
        received = (
            self.n_close_long * self.long_bid_current      # from closing longs
            + self.n_new_short * self.new_short_bid        # from new shorts
        ) * 100
        paid = self.n_transform * self.short_ask_current * 100

        self.roll_gross_credit = received - paid

        # Roll commissions (three orders)
        self.roll_commission = (
            ibkr_commission(self.n_close_long)   # sell original longs
            + ibkr_commission(self.n_transform)  # buy at original short strike
            + ibkr_commission(self.n_new_short)  # sell new shorts
        )

        self.roll_net_credit = self.roll_gross_credit - self.roll_commission
        self.roll_is_credit  = self.roll_net_credit > 0

        # Accumulated credit since inception
        self.accumulated_net_credit = self.entry_net_credit + self.roll_net_credit

        # New position: 2*N long at original_short, 2*M*N short at new_short
        self.new_lots = 2 * N

        # New upper break-even, accounting for all accumulated credit.
        # At expiry, when S > new_short_strike, total P&L = 0:
        #   accumulated_net_credit + new_lots * 100 * [(S - K2) - M*(S - K3)] = 0
        #   → S = [M*K3 - K2 + accumulated_per_share] / (M - 1)
        # where accumulated_per_share = accumulated_net_credit / (new_lots * 100)
        K2 = self.original_short_strike
        K3 = self.new_short_strike

        if M > 1:
            acc_per_share = self.accumulated_net_credit / (self.new_lots * 100)
            self.new_upper_BE = (M * K3 - K2 + acc_per_share) / (M - 1)
        else:
            self.new_upper_BE = float("inf")

        if self.underlying_price > 0 and not math.isinf(self.new_upper_BE):
            self.new_upper_BE_pct = (
                self.new_upper_BE / self.underlying_price - 1
            ) * 100
        else:
            self.new_upper_BE_pct = float("inf")

        # Max profit at expiry when price pins at new_short_strike (per new lot)
        # Net credit per new lot per share = accumulated / (new_lots * 100)
        acc_per_share = self.accumulated_net_credit / (self.new_lots * 100)
        self.new_max_profit_per_lot = (
            (K3 - K2) + acc_per_share
        ) * 100

        self.position_is_profitable_floor = self.accumulated_net_credit > 0

    def describe(self) -> dict:
        """Plain-dict summary suitable for JSON serialisation."""
        def _fmt(v: float, d: int = 2) -> float | None:
            return None if (math.isnan(v) or math.isinf(v)) else round(v, d)

        return {
            # ── Original position ──
            "original_long_strike":   self.original_long_strike,
            "original_short_strike":  self.original_short_strike,
            "ratio":                  f"1:{self.n_short}",
            "lots":                   self.lots,
            "entry_net_credit_$":     round(self.entry_net_credit, 2),
            # ── Current market prices used ──
            "underlying_price":       round(self.underlying_price, 2),
            "long_bid_current":       round(self.long_bid_current, 4),
            "short_ask_current":      round(self.short_ask_current, 4),
            "new_short_strike":       self.new_short_strike,
            "new_short_bid":          round(self.new_short_bid, 4),
            # ── Roll transaction ──
            "roll_contracts": {
                "sell_long":      self.n_close_long,
                "buy_transform":  self.n_transform,
                "sell_new_short": self.n_new_short,
            },
            "roll_gross_credit_$":    round(self.roll_gross_credit, 2),
            "roll_commission_$":      round(self.roll_commission, 2),
            "roll_net_credit_$":      round(self.roll_net_credit, 2),
            "roll_is_credit":         self.roll_is_credit,
            # ── New position after roll ──
            "new_long_strike":        self.original_short_strike,
            "new_short_strike_out":   self.new_short_strike,
            "new_lots":               self.new_lots,
            "accumulated_net_credit_$": round(self.accumulated_net_credit, 2),
            "position_is_profitable_floor": self.position_is_profitable_floor,
            # ── New profit/loss landmarks ──
            "new_upper_BE":           _fmt(self.new_upper_BE),
            "new_upper_BE_pct":       _fmt(self.new_upper_BE_pct, 1),
            "new_max_profit_per_lot_$": _fmt(self.new_max_profit_per_lot),
        }


# ---------------------------------------------------------------------------
# Advisor function
# ---------------------------------------------------------------------------

def analyze_roll(
    original_long_strike: float,
    original_short_strike: float,
    n_short: int,
    lots: int,
    entry_net_credit: float,
    chain_df,          # pd.DataFrame with columns: strike, bid, ask, valid
    underlying_price: float,
    max_new_gap: int = 2,
) -> list[RollScenario]:
    """
    Given an open position and a current option chain, return all viable
    roll scenarios ordered by new_upper_BE_pct (best probability first).

    Parameters
    ----------
    original_long_strike   : strike of the long leg in the current position
    original_short_strike  : strike of the short leg in the current position
    n_short                : M — the ratio (shorts per long); 2 or 3
    lots                   : N — number of long contracts currently held
    entry_net_credit       : $ net credit received when the position was opened
    chain_df               : current option chain (must include original strikes)
    underlying_price       : current price of the underlying
    max_new_gap            : max strike steps above original_short for new short

    Returns
    -------
    List of RollScenario sorted by new_upper_BE_pct descending.
    Includes all scenarios regardless of viability so the caller can
    display them and highlight the best ones.
    """
    import pandas as pd

    valid = (
        chain_df[chain_df["valid"]]
        .sort_values("strike")
        .reset_index(drop=True)
    )
    strikes = valid["strike"].tolist()

    # Look up current prices for the original strikes
    def _get_row(strike: float) -> dict | None:
        rows = valid[valid["strike"] == strike]
        if rows.empty:
            return None
        return rows.iloc[0].to_dict()

    long_row  = _get_row(original_long_strike)
    short_row = _get_row(original_short_strike)

    if long_row is None or short_row is None:
        return []

    long_bid_current  = float(long_row.get("bid", float("nan")))
    short_ask_current = float(short_row.get("ask", float("nan")))

    if math.isnan(long_bid_current) or math.isnan(short_ask_current):
        return []

    # Find the index of the original short strike in the valid chain
    try:
        short_idx = strikes.index(original_short_strike)
    except ValueError:
        return []

    scenarios: list[RollScenario] = []

    for gap in range(1, max_new_gap + 1):
        new_idx = short_idx + gap
        if new_idx >= len(strikes):
            break

        new_short_row = valid.iloc[new_idx]
        new_short_bid = float(new_short_row["bid"])

        if new_short_bid <= 0 or math.isnan(new_short_bid):
            continue

        scenario = RollScenario(
            original_long_strike=original_long_strike,
            original_short_strike=original_short_strike,
            n_short=n_short,
            lots=lots,
            entry_net_credit=entry_net_credit,
            underlying_price=underlying_price,
            long_bid_current=long_bid_current,
            short_ask_current=short_ask_current,
            new_short_strike=float(new_short_row["strike"]),
            new_short_bid=new_short_bid,
        )
        scenarios.append(scenario)

    # Sort: profitable floor first, then by new_upper_BE_pct descending
    scenarios.sort(
        key=lambda s: (
            s.position_is_profitable_floor,
            s.new_upper_BE_pct if not math.isinf(s.new_upper_BE_pct) else 0.0,
        ),
        reverse=True,
    )
    return scenarios
