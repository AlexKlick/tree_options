"""Discovery scan config: operator TOML -> validated, fail-closed.

Lives in ``~/.config/trex/discovery.toml`` (repo default under
``deploy/trex/``) — NEVER in ``plans/``: the plan glob would adopt it as
a trade plan. Unknown keys are refused (a typoed threshold must not
silently run with a default).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # py<3.11
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

# 71 monitor / 72 enter / 77 IbkrTrex library default — a collision makes
# two clients kick each other off the gateway mid-scan.
RESERVED_CLIENT_IDS = frozenset({71, 72, 73, 77})
DISCOVERY_CLIENT_ID = 74

TARGET_MODES = ("auto", "delta", "otm", "premium")


@dataclass(frozen=True)
class ScanConfig:
    underlyings: list[str]
    dte_min: int = 20
    dte_max: int = 60
    widths: list[float] = field(default_factory=lambda: [5.0, 10.0, 15.0, 20.0])
    target_mode: str = "auto"
    target_delta: float = 0.30
    target_otm_frac: float = 0.06
    delta_band: tuple[float, float] = (0.20, 0.45)
    min_debit: float = 0.15
    max_leg_spread_frac: float = 0.25
    max_quotes_per_underlying: int = 24  # gateway mkt-data line budget
    max_candidates_per_underlying: int = 3
    max_candidates_total: int = 12
    client_id: int = DISCOVERY_CLIENT_ID
    auto_scan_et: str = "16:11"  # serve-loop daily rescan (just after close)
    market_refresh_seconds: int = 60  # quote refresh cadence in serve loop

    @property
    def reserved_client_ids(self) -> frozenset[int]:
        return RESERVED_CLIENT_IDS


_FIELDS = {f for f in ScanConfig.__dataclass_fields__ if f != "reserved_client_ids"}


def _check(cfg: ScanConfig) -> None:
    if cfg.market_refresh_seconds <= 0:
        raise ValueError("market_refresh_seconds must be positive")
    if not cfg.underlyings:
        raise ValueError("underlyings must not be empty")
    if cfg.dte_min >= cfg.dte_max or cfg.dte_min < 0:
        raise ValueError("dte window must satisfy 0 <= dte_min < dte_max")
    if any(w <= 0 for w in cfg.widths):
        raise ValueError("widths must be positive")
    if len(set(cfg.widths)) != len(cfg.widths):
        raise ValueError("widths must be unique")
    if cfg.target_mode not in TARGET_MODES:
        raise ValueError(f"target_mode must be one of {TARGET_MODES}")
    if not 0 < cfg.target_delta < 1 or not 0 < cfg.target_otm_frac < 1:
        raise ValueError("target_delta/target_otm_frac must be in (0,1)")
    if not 0 < cfg.delta_band[0] < cfg.delta_band[1] < 1:
        raise ValueError("delta_band must be 0 < lo < hi < 1")
    if cfg.min_debit < 0:
        raise ValueError("min_debit must be >= 0")
    if not 0 < cfg.max_leg_spread_frac < 1:
        raise ValueError("max_leg_spread_frac must be in (0,1)")
    if cfg.client_id in RESERVED_CLIENT_IDS:
        raise ValueError(f"reserved clientId {cfg.client_id} (monitor/enter/library)")


def load_scan_config(path: Path) -> ScanConfig:
    if not path.exists():
        raise FileNotFoundError(f"discovery config missing: {path}")
    raw: dict[str, Any] = tomllib.loads(path.read_text())
    unknown = set(raw) - _FIELDS
    if unknown:
        raise ValueError(f"unknown key(s) {sorted(unknown)} in {path}")
    cfg = ScanConfig(
        underlyings=[str(u).upper() for u in raw.get("underlyings", [])],
        dte_min=int(raw.get("dte_min", 20)),
        dte_max=int(raw.get("dte_max", 60)),
        widths=[float(w) for w in raw.get("widths", [5.0, 10.0, 15.0, 20.0])],
        target_mode=str(raw.get("target_mode", "auto")),
        target_delta=float(raw.get("target_delta", 0.30)),
        target_otm_frac=float(raw.get("target_otm_frac", 0.06)),
        delta_band=tuple(float(x) for x in raw.get("delta_band", (0.20, 0.45))),  # type: ignore[arg-type]
        min_debit=float(raw.get("min_debit", 0.15)),
        max_leg_spread_frac=float(raw.get("max_leg_spread_frac", 0.25)),
        max_quotes_per_underlying=int(raw.get("max_quotes_per_underlying", 24)),
        max_candidates_per_underlying=int(raw.get("max_candidates_per_underlying", 3)),
        max_candidates_total=int(raw.get("max_candidates_total", 12)),
        client_id=int(raw.get("client_id", DISCOVERY_CLIENT_ID)),
        auto_scan_et=str(raw.get("auto_scan_et", "16:11")),
        market_refresh_seconds=int(raw.get("market_refresh_seconds", 60)),
    )
    _check(cfg)
    return cfg


def config_echo(cfg: ScanConfig) -> dict[str, Any]:
    """Page-safe projection for GET /api/discovery (no operational fields)."""
    return {
        "underlyings": list(cfg.underlyings),
        "dte_min": cfg.dte_min,
        "dte_max": cfg.dte_max,
        "widths": list(cfg.widths),
        "target_mode": cfg.target_mode,
        "target_delta": cfg.target_delta,
        "target_otm_frac": cfg.target_otm_frac,
        "delta_band": list(cfg.delta_band),
        "min_debit": cfg.min_debit,
        "max_leg_spread_frac": cfg.max_leg_spread_frac,
        "market_refresh_seconds": cfg.market_refresh_seconds,
        "max_candidates_per_underlying": cfg.max_candidates_per_underlying,
        "max_candidates_total": cfg.max_candidates_total,
    }
