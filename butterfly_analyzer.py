"""
Butterfly spread analyzer — Long Butterfly (Mariposa Comprada)

Structure:
  BUY  1 call at strike K1  (lower wing)
  SELL 2 calls at strike K2  (body — near ATM or OTM)
  BUY  1 call at strike K3  (upper wing)
  Symmetric: K2 - K1 = K3 - K2 = W  (wing width)

Entry price convention (real executable prices):
  Long legs (K1, K3): paid at ASK
  Short body (K2):    received at BID × 2

Net cash cost per share = ask(K1) + ask(K3) - 2 × bid(K2)

A "free" butterfly: net_cost ≤ 0
  - No cash outlay required at entry.
  - The entire [K1, K3] range is profitable at expiry
    (lower_BE ≤ K1 and upper_BE ≥ K3).
  - Max loss = commissions only (when spot ends outside the wings).

Commission model: IBKR fixed — $0.65/contract, min $1.00/order.
Three separate orders per lot: buy K1 (1c), sell K2 (2c), buy K3 (1c).

Score = –net_cost  (more credit → higher score → better butterfly).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from ratio_analyzer import ibkr_commission


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class ButterflySpread:
    """One symmetric long-butterfly candidate (calls)."""

    # ── Market inputs ──────────────────────────────────────────────────────────
    underlying_price: float
    lower_strike: float    # K1 — lower wing
    body_strike:  float    # K2 — body (sold ×2)
    upper_strike: float    # K3 — upper wing

    # Entry prices (ask to buy, bid to sell — real executable prices)
    lower_ask: float       # paid to open K1
    body_bid:  float       # credit received per body contract (×2)
    upper_ask: float       # paid to open K3

    # Closing prices for early exit / roll reference
    lower_bid: float = float("nan")
    body_ask:  float = float("nan")
    upper_bid: float = float("nan")

    # ── Derived (computed in __post_init__) ───────────────────────────────────
    wing_width:          float = field(init=False)
    net_cost:            float = field(init=False)   # $/share: + = debit, − = credit
    is_free:             bool  = field(init=False)

    lower_BE:            float = field(init=False)
    upper_BE:            float = field(init=False)

    max_profit_per_lot:  float = field(init=False)   # $ per lot when price pins at K2
    commission_1lot:     float = field(init=False)
    net_cost_1lot:       float = field(init=False)   # $ per lot after commissions (+ = pay)
    is_free_after_comm:  bool  = field(init=False)

    max_risk_1lot:       float = field(init=False)
    reward_risk:         float = field(init=False)
    body_pct:            float = field(init=False)   # body strike % above spot
    score:               float = field(init=False)

    def __post_init__(self) -> None:
        self.wing_width = round(self.body_strike - self.lower_strike, 8)

        # Net cost per share (positive = debit, negative = credit)
        self.net_cost = self.lower_ask + self.upper_ask - 2.0 * self.body_bid
        self.is_free  = self.net_cost <= 0.0

        # Break-even prices at expiry
        #   P&L(S) = max(S-K1, 0) - 2·max(S-K2, 0) + max(S-K3, 0) - net_cost
        #   When net_cost > 0: lower_BE = K1 + cost, upper_BE = K3 - cost
        #   When free (net_cost ≤ 0): the entire [K1,K3] range is profitable
        cost_floor = max(self.net_cost, 0.0)
        self.lower_BE = self.lower_strike + cost_floor
        self.upper_BE = self.upper_strike - cost_floor

        # Max profit when price = K2 at expiry: (K2 - K1) - net_cost $/share
        self.max_profit_per_lot = (self.wing_width - self.net_cost) * 100.0

        # IBKR commissions: 3 orders per lot
        self.commission_1lot = (
            ibkr_commission(1)   # buy lower wing (1 contract)
            + ibkr_commission(2) # sell body      (2 contracts)
            + ibkr_commission(1) # buy upper wing (1 contract)
        )

        # Net cash flow per lot (positive = we pay, negative = we receive)
        self.net_cost_1lot = self.net_cost * 100.0 + self.commission_1lot
        self.is_free_after_comm = self.net_cost_1lot <= 0.0

        # Max risk per lot: initial outlay + commissions (if free, only commissions)
        if self.is_free:
            self.max_risk_1lot = self.commission_1lot
        else:
            self.max_risk_1lot = self.net_cost * 100.0 + self.commission_1lot

        # Reward / risk ratio
        if self.max_risk_1lot > 0:
            self.reward_risk = self.max_profit_per_lot / self.max_risk_1lot
        else:
            self.reward_risk = float("inf")

        # Body % above underlying price
        if self.underlying_price > 0:
            self.body_pct = (self.body_strike / self.underlying_price - 1.0) * 100.0
        else:
            self.body_pct = float("nan")

        # Score: –net_cost so that more credit → higher score → sorts first
        self.score = -self.net_cost

    # ── Validation ────────────────────────────────────────────────────────────

    @property
    def is_valid(self) -> bool:
        return (
            not math.isnan(self.lower_ask)
            and not math.isnan(self.body_bid)
            and not math.isnan(self.upper_ask)
            and self.lower_ask > 0
            and self.body_bid  > 0
            and self.upper_ask > 0
            and self.wing_width > 0
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def net_cost_for_lots(self, n_lots: int) -> float:
        """Net $ cost for n_lots (positive = we pay, negative = we receive)."""
        gross = self.net_cost * 100.0 * n_lots
        comm  = (
            ibkr_commission(n_lots)
            + ibkr_commission(2 * n_lots)
            + ibkr_commission(n_lots)
        )
        return gross + comm

    def describe(self, lots: int = 1) -> dict:
        """Plain-dict summary suitable for JSON serialisation."""
        def _fmt(v: float, d: int = 4):
            return None if (math.isnan(v) or math.isinf(v)) else round(v, d)

        net_lots = self.net_cost_for_lots(lots) if lots != 1 else None

        result: dict = {
            "lower_strike":          self.lower_strike,
            "body_strike":           self.body_strike,
            "upper_strike":          self.upper_strike,
            "wing_width":            round(self.wing_width, 2),
            "underlying_price":      round(self.underlying_price, 2),
            # ── Entry prices ──
            "lower_ask":             round(self.lower_ask, 4),
            "body_bid":              round(self.body_bid, 4),
            "upper_ask":             round(self.upper_ask, 4),
            # ── Closing prices ──
            "lower_bid":             _fmt(self.lower_bid, 4),
            "body_ask":              _fmt(self.body_ask, 4),
            "upper_bid":             _fmt(self.upper_bid, 4),
            # ── Cost ──
            "net_cost":              round(self.net_cost, 4),
            "net_cost_$":            round(self.net_cost * 100.0, 2),
            "commission_1lot_$":     round(self.commission_1lot, 2),
            "net_cost_1lot_$":       round(self.net_cost_1lot, 2),
            # ── Classification ──
            "is_free":               self.is_free,
            "is_free_after_comm":    self.is_free_after_comm,
            # ── P&L landmarks ──
            "lower_BE":              _fmt(self.lower_BE, 2),
            "upper_BE":              _fmt(self.upper_BE, 2),
            "max_profit_strike":     self.body_strike,
            "max_profit_1lot_$":     round(self.max_profit_per_lot, 2),
            "max_risk_1lot_$":       round(self.max_risk_1lot, 2),
            "reward_risk":           _fmt(self.reward_risk, 1),
            # ── Position context ──
            "body_pct":              _fmt(self.body_pct, 1),
            "score":                 _fmt(self.score, 4),
        }

        if net_lots is not None:
            result[f"net_cost_{lots}lot_$"] = round(net_lots, 2)

        return result


# ---------------------------------------------------------------------------
# Scanner function
# ---------------------------------------------------------------------------

def find_butterfly_spreads(
    chain_df: pd.DataFrame,
    underlying_price: float,
    max_net_cost: float = 0.05,
    atm_band_pct: float = 30.0,
    min_wing_width: float = 0.5,
    max_wing_width: float | None = None,
    top_n: int = 20,
    lots: int = 1,
) -> list[ButterflySpread]:
    """
    Scan *chain_df* for long butterfly opportunities.

    Parameters
    ----------
    chain_df        : DataFrame with columns strike, bid, ask, mid, valid.
    underlying_price: current spot price of the underlying.
    max_net_cost    : $/share upper limit on net debit — use 0.0 for free-only.
    atm_band_pct    : body strike must be within [spot, spot*(1+X%)] above spot.
    min_wing_width  : minimum $ distance between adjacent strikes (K2-K1).
    max_wing_width  : optional maximum wing width in $.
    top_n           : maximum results to return.
    lots            : reference lot size (only used to label the n-lot cost key).

    Returns
    -------
    List of ButterflySpread sorted best-first:
      1. is_free True first
      2. net_cost ascending (most credit / least debit)
      3. max_profit_per_lot descending
    """
    valid = (
        chain_df[chain_df["valid"]]
        .sort_values("strike")
        .reset_index(drop=True)
    )

    if len(valid) < 3:
        return []

    # Strike lookup: strike -> (bid, ask)
    strike_data: dict[float, tuple[float, float]] = {}
    for _, row in valid.iterrows():
        strike_data[float(row["strike"])] = (
            float(row["bid"]),
            float(row["ask"]),
        )

    strikes_sorted = sorted(strike_data.keys())

    # Body filter range: only scan body strikes near spot
    if underlying_price > 0:
        body_min = underlying_price * 0.85
        body_max = underlying_price * (1.0 + atm_band_pct / 100.0)
    else:
        body_min = 0.0
        body_max = float("inf")

    candidates: list[ButterflySpread] = []

    # For each candidate body K2, look for lower wing K1 and matching upper K3
    for K2 in strikes_sorted:
        if K2 < body_min or K2 > body_max:
            continue

        k2_bid, k2_ask = strike_data[K2]
        if k2_bid <= 0 or math.isnan(k2_bid):
            continue

        for K1 in strikes_sorted:
            if K1 >= K2:
                break

            W = K2 - K1
            if W < min_wing_width - 1e-6:
                continue
            if max_wing_width is not None and W > max_wing_width + 1e-6:
                continue

            # Symmetric upper wing must also exist in the strike list
            K3_target = K2 + W
            K3: float | None = None
            for s in strikes_sorted:
                if abs(s - K3_target) < 0.01:
                    K3 = s
                    break
            if K3 is None:
                continue

            k1_bid, k1_ask = strike_data[K1]
            k3_bid, k3_ask = strike_data[K3]

            if k1_ask <= 0 or math.isnan(k1_ask):
                continue
            if k3_ask <= 0 or math.isnan(k3_ask):
                continue

            spread = ButterflySpread(
                underlying_price=underlying_price,
                lower_strike=K1,
                body_strike=K2,
                upper_strike=K3,
                lower_ask=k1_ask,
                body_bid=k2_bid,
                upper_ask=k3_ask,
                lower_bid=k1_bid,
                body_ask=k2_ask,
                upper_bid=k3_bid,
            )

            if not spread.is_valid:
                continue

            if spread.net_cost > max_net_cost + 1e-6:
                continue

            candidates.append(spread)

    # Sort: free first → most credit → highest max_profit
    candidates.sort(
        key=lambda s: (
            not s.is_free,            # False (0) = free first
            s.net_cost,               # ascending: most credit first
            -s.max_profit_per_lot,    # descending: higher profit first
        )
    )

    return candidates[:top_n]
