"""The sealed AI-BEAR-001 parameters.

The JSON file is sealed by its sha256 in ``SEALED_PARAMS_SHA256``, and the
pre-registration ``docs/desk/AI-BEAR-001.md`` quotes the same digest. Any
edit to the file changes the digest, so the scorer refuses to run until the
change is reviewed and the digest is resealed. Thresholds are kept as
decimal strings in the file and become floats only here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_options.desk.paths import repo_root

PARAMS_RELPATH = Path("data/desk/aibear/AI-BEAR-001.params.json")
#: sha256 of the sealed parameter file; set when AI-BEAR-001 was sealed
SEALED_PARAMS_SHA256 = "072ab78199ba2d33dd63f639d6682e35e2d771ded2113b769497ee33559ecd53"

#: AI-BEAR-002 as amended (AMENDMENT 1): the forward-only redesign the monitor runs
FORWARD_PARAMS_RELPATH = Path("data/desk/aibear/AI-BEAR-002-A1.params.json")
SEALED_FORWARD_PARAMS_SHA256 = "1ec43b1b4f2f42e2aaf6b9251dd302e0ef6ccdda4e4ffbf45b432fea3ea9f685"

STATES = ("BULL", "TOPPING", "BEAR", "RELIEF", "CAPITULATION")
NOT_EVALUABLE = "NOT_EVALUABLE"


class ParamsError(ValueError):
    """The parameter file is missing, malformed, or not the sealed one."""


@dataclass(frozen=True)
class RegimeParams:
    ma_fast: int
    ma_slow: int
    slope_lag: int
    high_window: int
    mom_window: int
    vix_median_window: int
    vix_max_staleness_days: int
    bear_drawdown: float
    topping_drawdown: float
    capitulation_drawdown: float
    capitulation_momentum: float
    capitulation_vix_ratio: float

    @property
    def warmup(self) -> int:
        """Observations before the first evaluable index (0-based index)."""
        return max(self.high_window - 1, self.ma_slow - 1 + self.slope_lag, self.mom_window)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RegimeParams:
        ints = (
            "ma_fast",
            "ma_slow",
            "slope_lag",
            "high_window",
            "mom_window",
            "vix_median_window",
            "vix_max_staleness_days",
        )
        floats = (
            "bear_drawdown",
            "topping_drawdown",
            "capitulation_drawdown",
            "capitulation_momentum",
            "capitulation_vix_ratio",
        )
        try:
            kw: dict[str, Any] = {k: int(d[k]) for k in ints}
            kw.update({k: float(d[k]) for k in floats})
        except (KeyError, TypeError, ValueError) as exc:
            raise ParamsError(f"regime params: {exc}") from None
        return cls(**kw)


def params_path() -> Path:
    return repo_root() / PARAMS_RELPATH


def forward_params_path() -> Path:
    return repo_root() / FORWARD_PARAMS_RELPATH


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_params(
    path: Path | None = None,
    *,
    require_sealed: bool = False,
    sealed_sha256: str = SEALED_PARAMS_SHA256,
) -> dict[str, Any]:
    """The parameter document. ``require_sealed`` refuses a file whose digest
    is not ``sealed_sha256`` (AI-BEAR-001's by default)."""
    p = path or params_path()
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise ParamsError(f"cannot read {p}: {exc.strerror}") from None
    if require_sealed:
        digest = hashlib.sha256(raw).hexdigest()
        if digest != sealed_sha256:
            raise ParamsError(
                f"{p.name} sha256 {digest[:12]} is not the sealed digest {sealed_sha256[:12]}"
            )
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParamsError(f"{p.name}: {exc}") from None
    if not isinstance(doc, dict) or doc.get("schema") != "aibear-params/1":
        raise ParamsError(f"{p.name}: not an aibear-params/1 document")
    return doc


def regime_params(doc: dict[str, Any]) -> RegimeParams:
    return RegimeParams.from_dict(doc["regime"])
