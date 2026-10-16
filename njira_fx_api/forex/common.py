"""
Shared forex helpers (symbol normalization + validation patterns).

The legacy Deriv-era ``frx``/``FRX`` prefix is stripped so the API can accept
both the old UI selector symbols (``frxEURUSD``) and plain provider symbols
(``EURUSD``) interchangeably. This mirrors ``forex.sync.management.commands.
sync_engine._normalize_symbols`` but lives here as a shared, importable helper
so views and other modules do not reach into a management command (per the
backend-dev task contract).
"""
from __future__ import annotations

import re

#: Allowed symbol characters after normalization (1-20 chars of A-Z, 0-9, _).
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9_]{1,20}$")


def normalize_symbol(raw: str) -> str:
    """
    Normalize a raw symbol to its plain provider form.

    Uppercases the input and strips the legacy Deriv-era ``FRX`` prefix
    (covers both ``frx`` and ``FRX`` because the input is uppercased first).
    No other transformation is applied.
    """
    if not isinstance(raw, str):
        return ""
    symbol = raw.strip().upper()
    if symbol.startswith("FRX"):
        symbol = symbol[3:]
    return symbol


def is_valid_symbol(symbol: str) -> bool:
    """Return ``True`` when ``symbol`` matches the allowlist pattern."""
    return isinstance(symbol, str) and bool(SYMBOL_PATTERN.fullmatch(symbol))
