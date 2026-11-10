"""Broker-free, read-only quote selection for TREX market views.

The authoritative market.json snapshot preserves the most recently recorded
per-symbol source timestamp, including when the last refresh failed. A
per-symbol TTL cache is consulted only if no usable snapshot quote exists.
No new fetch, snapshot rewrite, or synthetic price is allowed in this layer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

_QUOTE_SYMBOL = re.compile(r"[A-Z]+(?:\.[A-Z]+)?\Z")


def validate_quote_symbol(raw: object) -> str:
    """Canonical, path-safe symbol accepted by the read-only MCP quote lane."""
    if not isinstance(raw, str):
        raise ValueError("symbol must be text")
    symbol = raw.upper()
    if len(symbol) > 6 or _QUOTE_SYMBOL.fullmatch(symbol) is None:
        raise ValueError("symbol must be 1-6 ASCII letters, optional internal class period")
    return symbol


def read_market_quote(
    snapshot_path: Path,
    symbol: str,
    cache_lookup: Callable[[], dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Read one authoritative snapshot quote, otherwise call cache once.

    An absent, malformed or partially replaced snapshot is not a reason to
    guess a quote. The caller's established TTL cache owns the fallback.
    JSON decoding happens at most once, with no extra existence syscall.
    """
    try:
        snapshot = json.loads(snapshot_path.read_text())
    except (OSError, ValueError):
        snapshot = None
    if isinstance(snapshot, dict):
        symbols = snapshot.get("symbols")
        if isinstance(symbols, dict):
            quote = symbols.get(symbol)
            if isinstance(quote, dict):
                return quote
    cached = cache_lookup()
    return cached if isinstance(cached, dict) else None
