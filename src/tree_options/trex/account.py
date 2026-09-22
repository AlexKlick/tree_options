"""Account-equity snapshot payload (pure; the broker read lives in ibkr).

The monitor (and later the discovery runner) writes ``account.json`` into
a run directory every few ticks; the broker-free web lane reads the
freshest copy across run dirs. Money rides as Decimal-strings per the
repo convention; ``ts`` is when the broker values were OBSERVED and is
never re-stamped by a copier.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.trex.clock import ET


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    net_liquidation: Decimal
    cash: Decimal
    buying_power: Decimal
    currency: str
    ts: datetime  # when the broker values were observed

    def to_payload(self) -> dict[str, str]:
        return {
            "account_id": self.account_id,
            "net_liquidation": str(self.net_liquidation),
            "cash": str(self.cash),
            "buying_power": str(self.buying_power),
            "currency": self.currency,
            "ts": self.ts.isoformat(),
        }

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> AccountSnapshot:
        return cls(
            account_id=str(raw["account_id"]),
            net_liquidation=Decimal(str(raw["net_liquidation"])),
            cash=Decimal(str(raw["cash"])),
            buying_power=Decimal(str(raw["buying_power"])),
            currency=str(raw.get("currency", "USD")),
            ts=datetime.fromisoformat(str(raw["ts"])),
        )


def write_account(path: Path, snapshot: AccountSnapshot) -> None:
    """Atomic account.json write (tmp + os.replace, like book.json)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot.to_payload()) + "\n")
    os.replace(tmp, path)


def load_account(path: Path) -> dict[str, Any] | None:
    """The payload dict, or None when missing/unreadable/corrupt."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) and "ts" in raw else None


def account_age_seconds(payload: dict[str, Any], now: datetime | None = None) -> int | None:
    """Seconds since the broker observation; negative (clock skew) clamps 0."""
    raw = payload.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    ref = now or datetime.now(ET)
    return max(0, int((ref - ts).total_seconds()))


def freshest(paths: list[Path]) -> tuple[Path, dict[str, Any]] | None:
    """Newest valid payload across candidate account.json paths.

    Tolerant: missing/corrupt files are skipped, not fatal. The caller
    scopes by account identity if several accounts exist.
    """
    best: tuple[Path, dict[str, Any], datetime] | None = None
    for path in paths:
        payload = load_account(path)
        if payload is None:
            continue
        try:
            ts = datetime.fromisoformat(str(payload["ts"]))
        except (KeyError, ValueError):
            continue
        if best is None or ts > best[2]:
            best = (path, payload, ts)
    if best is None:
        return None
    return best[0], best[1]
