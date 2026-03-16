"""
Persistent storage for active ratio spread positions.

A position is opened when the user declares they took a trade from the
screener output.  The system then monitors it and raises alerts when the
underlying price moves into warning or danger territory.

Alert levels (compared against the underlying's current price):
  ok       — price ≤ short_strike (in or below the max-profit zone)
  warning  — short_strike < price ≤ upper_BE  (past max-profit, still
              profitable at expiry but declining; consider rolling)
  alert    — price > upper_BE  (past break-even, position loses money
              at expiry if not rolled; roll urgently)

Positions are stored as a JSON file so they survive restarts.
"""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from dataclasses import dataclass, asdict, field
from datetime import date, datetime

from ratio_analyzer import ibkr_commission

_DATA_DIR       = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
_POSITIONS_FILE = os.path.join(_DATA_DIR, "positions.json")

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class Position:
    symbol: str
    expiration: str          # YYYYMMDD
    long_strike: float
    short_strike: float
    n_short: int             # M: shorts per long (2 or 3)
    lots: int                # N: number of long contracts
    long_ask: float          # actual fill price paid for long leg
    short_bid: float         # actual fill price received for short leg

    # computed at entry
    gross_credit: float = field(init=False)
    commission: float = field(init=False)
    entry_net_credit: float = field(init=False)   # $ after commissions
    upper_BE: float = field(init=False)
    max_profit_price: float = field(init=False)   # = short_strike (pin point)
    max_profit_per_lot: float = field(init=False) # $ if price pins at short_strike

    # metadata
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    entry_date: str = field(default_factory=lambda: date.today().isoformat())
    status: str = "active"   # "active" | "closed"
    note: str = ""

    def __post_init__(self) -> None:
        self.gross_credit = self.short_bid * self.n_short - self.long_ask
        self.commission = (
            ibkr_commission(self.lots)
            + ibkr_commission(self.lots * self.n_short)
        )
        self.entry_net_credit = self.gross_credit * 100 * self.lots - self.commission

        # upper break-even: (n*short - long + gross_credit) / (n - 1)
        if self.n_short > 1:
            self.upper_BE = (
                self.n_short * self.short_strike
                - self.long_strike
                + self.gross_credit
            ) / (self.n_short - 1)
        else:
            self.upper_BE = float("inf")

        self.max_profit_price = self.short_strike
        self.max_profit_per_lot = (
            (self.short_strike - self.long_strike) + self.gross_credit
        ) * 100

    def dte(self) -> int:
        """Days to expiration from today."""
        try:
            exp = datetime.strptime(self.expiration, "%Y%m%d").date()
            return (exp - date.today()).days
        except ValueError:
            return -1

    def alert_level(self, current_price: float) -> str:
        """
        Return the alert level given the current underlying price.

          ok      : price ≤ short_strike
          warning : short_strike < price ≤ upper_BE
          alert   : price > upper_BE
        """
        if math.isnan(current_price) or current_price <= 0:
            return "unknown"
        if current_price <= self.short_strike:
            return "ok"
        if math.isinf(self.upper_BE) or current_price <= self.upper_BE:
            return "warning"
        return "alert"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["upper_BE"] = None if math.isinf(self.upper_BE) else round(self.upper_BE, 2)
        d["entry_net_credit"] = round(self.entry_net_credit, 2)
        d["commission"] = round(self.commission, 2)
        d["gross_credit"] = round(self.gross_credit, 4)
        d["max_profit_per_lot"] = round(self.max_profit_per_lot, 2)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        # Reconstruct from stored dict — skip computed fields, re-derive in __post_init__
        p = cls.__new__(cls)
        p.id           = d["id"]
        p.symbol       = d["symbol"]
        p.expiration   = d["expiration"]
        p.long_strike  = float(d["long_strike"])
        p.short_strike = float(d["short_strike"])
        p.n_short      = int(d["n_short"])
        p.lots         = int(d["lots"])
        p.long_ask     = float(d["long_ask"])
        p.short_bid    = float(d["short_bid"])
        p.entry_date   = d.get("entry_date", "")
        p.status       = d.get("status", "active")
        p.note         = d.get("note", "")
        p.__post_init__()
        return p


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_all() -> list[Position]:
    try:
        with open(_POSITIONS_FILE) as f:
            raw = json.load(f)
        return [Position.from_dict(r) for r in raw if isinstance(r, dict)]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return []


def _save_all(positions: list[Position]) -> None:
    with open(_POSITIONS_FILE, "w") as f:
        json.dump([p.to_dict() for p in positions], f, indent=2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_positions(status: str | None = "active") -> list[Position]:
    """Return positions filtered by status ('active', 'closed', or None for all)."""
    with _lock:
        all_pos = _load_all()
    if status is None:
        return all_pos
    return [p for p in all_pos if p.status == status]


def add_position(
    symbol: str,
    expiration: str,
    long_strike: float,
    short_strike: float,
    n_short: int,
    lots: int,
    long_ask: float,
    short_bid: float,
    note: str = "",
) -> Position:
    """Create and persist a new active position."""
    p = Position(
        symbol=symbol,
        expiration=expiration,
        long_strike=long_strike,
        short_strike=short_strike,
        n_short=n_short,
        lots=lots,
        long_ask=long_ask,
        short_bid=short_bid,
        note=note,
    )
    with _lock:
        positions = _load_all()
        positions.append(p)
        _save_all(positions)
    return p


def close_position(position_id: str) -> bool:
    """Mark a position as closed. Returns True if found."""
    with _lock:
        positions = _load_all()
        for p in positions:
            if p.id == position_id:
                p.status = "closed"
                _save_all(positions)
                return True
    return False


def get_position(position_id: str) -> Position | None:
    with _lock:
        for p in _load_all():
            if p.id == position_id:
                return p
    return None
