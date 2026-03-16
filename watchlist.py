"""Persistent symbol watchlist stored as JSON."""

from __future__ import annotations

import json
import os
import threading

_WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
_DEFAULT_SYMBOLS = ["DLO", "NU", "MARA", "RIOT", "PLTR", "SOFI", "HOOD"]

_lock = threading.Lock()


def _load_raw() -> list[str]:
    try:
        with open(_WATCHLIST_FILE) as f:
            data = json.load(f)
            if isinstance(data, list):
                return [s.upper() for s in data if isinstance(s, str)]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return list(_DEFAULT_SYMBOLS)


def _save_raw(symbols: list[str]) -> None:
    with open(_WATCHLIST_FILE, "w") as f:
        json.dump(symbols, f, indent=2)


def get_symbols() -> list[str]:
    with _lock:
        return list(_load_raw())


def add_symbol(symbol: str) -> list[str]:
    symbol = symbol.upper().strip()
    with _lock:
        symbols = _load_raw()
        if symbol not in symbols:
            symbols.append(symbol)
            _save_raw(symbols)
        return list(symbols)


def remove_symbol(symbol: str) -> list[str]:
    symbol = symbol.upper().strip()
    with _lock:
        symbols = _load_raw()
        symbols = [s for s in symbols if s != symbol]
        _save_raw(symbols)
        return list(symbols)
