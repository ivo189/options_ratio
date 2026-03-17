"""Persistent configuration for the automatic trading bot.

Settings are stored in bot_config.json next to this file.
All fields have safe defaults so a missing file is equivalent to
a freshly-initialised config.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "bot_config.json"


@dataclass
class BotConfig:
    # ── Auto-open rules ───────────────────────────────────────────────────────
    auto_open: bool = False
    """Enable automatic position opening when conditions are met."""

    min_otm_pct: float = 10.0
    """Long strike must be at least this % above the current spot price.
    E.g. 10 → long strike ≥ spot × 1.10."""

    target_ratio: int = 2
    """Short contracts per long leg (1:N). Currently only 1:2 is used."""

    min_net_credit: float = 0.0
    """Minimum net credit per share (after commissions) to open a position.
    0 means any positive credit qualifies."""

    # ── Notifications (Telegram) ──────────────────────────────────────────────
    telegram_token: str = ""
    """Telegram bot token (BotFather). Stored locally, never sent to the UI."""

    telegram_chat_id: str = ""
    """Telegram chat or group ID that receives notifications."""

    notify_be_breach: bool = True
    """Send a Telegram alert when the underlying crosses the upper break-even."""

    notify_open: bool = True
    """Send a Telegram message when the bot opens a position automatically."""

    # ── Future: automatic roll / close (disabled for now) ────────────────────
    auto_roll: bool = False
    """Not implemented yet. Roll positions automatically when BE is breached."""

    auto_close_expired: bool = False
    """Not implemented yet. Register expired positions automatically at 16:05 ET."""

    # ── Internal helpers ─────────────────────────────────────────────────────

    def save(self) -> None:
        _CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls) -> "BotConfig":
        if not _CONFIG_PATH.exists():
            return cls()
        try:
            raw = json.loads(_CONFIG_PATH.read_text())
            valid = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
            return cls(**valid)
        except Exception:
            return cls()

    def to_public_dict(self) -> dict:
        """Return config without the telegram token (safe to send to the browser)."""
        d = asdict(self)
        d["telegram_token_set"] = bool(d.pop("telegram_token"))
        return d
