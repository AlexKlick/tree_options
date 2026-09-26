"""Forecast sources (RL-3) — the registry and the two series loaders.

Loaders return ``ForecastSeries | ForecastRefusal`` — a refusal is a
VALUE (the codebase discipline): the HTTP layer maps it to a typed 400
pre-write, the worker publishes it as a content-bound result.

Session-authority contract (the 2025-01-09 lesson): horizon arithmetic
uses the SOURCE'S OWN observed dates INTERSECTED with the repo's
closure-corrected session authority (the trex calendar DATA file — read
as bytes, never a trex code import). Verified live cases: the vendor
VIX.csv carries rows on 2025-01-09 (NYSE Carter-day closure) AND
2025-01-20 (MLK) while the authority treats both as non-sessions; a
vendor row on a non-session date is EXCLUDED, counted, and listed in
the series summary — visible, never silently consumed and never
silently dropped.

Provenance contract: ``provenance.jsonl`` sha256 entries hash the
VENDOR BODY; ``series_sha256`` hashes the STORED CSV bytes. They differ
by design (render vs vendor bytes); both are recorded and the
distinction is declared. The provenance lookup takes the LATEST line
matching ``source == name`` with a successful status — the file's last
line can be another instrument.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

from tree_options.desk.indices import read_store
from tree_options.research.forecast.contracts import ForecastSourceId
from tree_options.research.forecast.refusal_codes import (
    FORECAST_SOURCE_DRIFT,
    FORECAST_SOURCE_INVALID,
    ForecastRefusal,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_FIXTURES = _REPO_ROOT / "data" / "research" / "fixtures"
_SYNTHETIC_FIXTURE = _FIXTURES / "synthetic-forecast-v1.json"
#: The closure-corrected session authority (DATA file; its sha is bound
#: into every index ForecastSeries so the intersection is reproducible).
_SESSION_AUTHORITY = (
    _REPO_ROOT / "data" / "calendar" / "trex"
    / "nyse_sessions_2018_01_02_2028_12_29.json"
)
#: Declared evaluation scope: the pinned research calendar's own range.
SCOPE_START: Final[date] = date(2018, 1, 2)
SCOPE_END: Final[date] = date(2026, 12, 31)

#: The one canonical interval-semantics copy, served by the API and
#: rendered verbatim by the SPA (single source; audited to never claim
#: calibration).
INTERVAL_SEMANTICS: Final[str] = (
    "Bands are quantiles of the modeled h-session-ahead distribution of "
    "the index level: the central 90% band runs from the 5th to the 95th "
    "percentile. This is a nominal 90% prediction interval for the "
    "closing level at the target session — NOT a confidence interval "
    "for a mean, and NOT a claim of calibration. Calibration evidence "
    "is stated separately as empirical coverage over the evaluated "
    "rolling origins, with a Wilson 95% interval (binomial "
    "approximation; time-ordered origins, dependence not captured) and "
    "a block-bootstrap sensitivity."
)

_SUCCESS_STATUSES: Final[frozenset[str]] = frozenset(
    {"new", "updated", "revised", "unchanged"})


@dataclass(frozen=True)
class SourceDescriptor:
    source_id: ForecastSourceId
    label: str
    basis: str
    grid_basis: str
    enabled_horizons: tuple[int, ...]
    listed_horizons: tuple[int, ...]


#: Registry policy in one place: the synthetic lane enables h=5
#: (machinery validation only — its receipts can never qualify the
#: index lane); the VIX lane enables h=5 and h=20 with 63/126 listed
#: but disabled (illustrative only, per handoff §11).
SOURCE_REGISTRY: Final[Mapping[ForecastSourceId, SourceDescriptor]] = {
    ForecastSourceId.SYNTHETIC: SourceDescriptor(
        source_id=ForecastSourceId.SYNTHETIC,
        label="Synthetic forecast fixture (machinery validation)",
        basis="frozen fixture, sha-pinned",
        grid_basis="synthetic pinned-calendar sessions",
        enabled_horizons=(5,),
        listed_horizons=(5,),
    ),
    ForecastSourceId.INDEX_VIX: SourceDescriptor(
        source_id=ForecastSourceId.INDEX_VIX,
        label="VIX (CBOE via desk-store)",
        basis="desk-store latest revision (latest-vintage retrospective)",
        grid_basis="observed vendor dates intersected with "
                   "closure-corrected sessions",
        enabled_horizons=(5, 20),
        listed_horizons=(5, 20, 63, 126),
    ),
}


@dataclass(frozen=True)
class ForecastSeries:
    """A validated, identity-bound series on its corrected session grid."""

    source_id: ForecastSourceId
    sessions: tuple[date, ...]
    closes: tuple[float, ...]
    series_sha256: str
    basis: str
    grid_basis: str
    provenance: Mapping[str, Any]
    #: rows dropped with reasons, listed by ISO date — visible, never
    #: silent (e.g. {"non_session": ["2025-01-09", "2025-01-20"]}).
    excluded_rows: Mapping[str, tuple[str, ...]]
    n_source_rows: int

    def summary(self) -> dict[str, Any]:
        return {
            "n_sessions": len(self.sessions),
            "first_session": (
                self.sessions[0].isoformat() if self.sessions else None),
            "last_session": (
                self.sessions[-1].isoformat() if self.sessions else None),
            "series_sha256": self.series_sha256,
            "basis": self.basis,
            "grid_basis": self.grid_basis,
            "provenance": dict(self.provenance),
            "excluded_rows": {k: list(v)
                              for k, v in self.excluded_rows.items()},
            "n_source_rows": self.n_source_rows,
        }


def load_synthetic(
    fixtures_dir: Path | None = None,
) -> ForecastSeries | ForecastRefusal:
    """The frozen synthetic fixture, sha-verified against its sidecar.

    The fixture IS truth (a committed immutable artifact); the sidecar
    makes tampering detectable. Fixture sessions are used as declared —
    the synthetic lane does not intersect a session authority (its
    sessions come from the pinned calendar directly).
    """
    fixture = (
        fixtures_dir / "synthetic-forecast-v1.json"
        if fixtures_dir is not None else _SYNTHETIC_FIXTURE
    )
    sidecar = fixture.with_name(fixture.name + ".sha256")
    if not fixture.is_file():
        return ForecastRefusal(
            code=FORECAST_SOURCE_INVALID,
            message=f"synthetic fixture absent: {fixture}",
        )
    body = fixture.read_bytes()
    series_sha = hashlib.sha256(body).hexdigest()
    if sidecar.is_file():
        expected = sidecar.read_text().strip()
        if expected != series_sha:
            return ForecastRefusal(
                code=FORECAST_SOURCE_DRIFT,
                message=(
                    f"synthetic fixture sha drift: sidecar pins "
                    f"{expected}, file hashes {series_sha} — the frozen "
                    f"fixture changed; it is truth, so update the sidecar "
                    f"deliberately or restore the bytes"),
                payload={"sidecar_sha256": expected,
                         "file_sha256": series_sha},
            )
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as exc:
        return ForecastRefusal(
            code=FORECAST_SOURCE_INVALID,
            message=f"synthetic fixture is not JSON: {exc}",
        )
    sessions_raw = doc.get("sessions", [])
    levels_raw = doc.get("levels", [])
    if len(sessions_raw) != len(levels_raw) or not sessions_raw:
        return ForecastRefusal(
            code=FORECAST_SOURCE_INVALID,
            message="synthetic fixture sessions/levels length mismatch",
        )
    sessions: list[date] = []
    try:
        sessions = [date.fromisoformat(str(s)) for s in sessions_raw]
    except ValueError as exc:
        return ForecastRefusal(
            code=FORECAST_SOURCE_INVALID,
            message=f"synthetic fixture has a non-ISO session: {exc}",
        )
    closes: list[float] = []
    for v in levels_raw:
        try:
            closes.append(float(v))
        except (TypeError, ValueError):
            return ForecastRefusal(
                code=FORECAST_SOURCE_INVALID,
                message=f"synthetic fixture has a non-numeric level: {v!r}",
            )
    bad = _validate_grid(sessions, closes)
    if bad is not None:
        return ForecastRefusal(code=FORECAST_SOURCE_INVALID, message=bad)
    descriptor = SOURCE_REGISTRY[ForecastSourceId.SYNTHETIC]
    return ForecastSeries(
        source_id=ForecastSourceId.SYNTHETIC,
        sessions=tuple(sessions),
        closes=tuple(closes),
        series_sha256=series_sha,
        basis=descriptor.basis,
        grid_basis=descriptor.grid_basis,
        provenance={"kind": "synthetic-fixture",
                    "generator": doc.get("generator", {})},
        excluded_rows={},
        n_source_rows=len(sessions),
    )


def load_index(
    name: str = "VIX",
    *,
    store_root: Path | None = None,
) -> ForecastSeries | ForecastRefusal:
    """A desk-store index series on its corrected session grid.

    Read-only over the desk evidence store (``read_store``; the
    non-overlap guard covers writes). Validation: strictly increasing
    unique dates, finite positive closes, scope [2018-01-02,
    2026-12-31], observed dates intersected with the closure-corrected
    session authority — non-session rows are EXCLUDED with listed dates.
    """
    root = (
        store_root if store_root is not None
        else _REPO_ROOT / "artifacts" / "desk-store" / "indices"
    )
    path = root / f"{name}.csv"
    if not path.is_file():
        return ForecastRefusal(
            code=FORECAST_SOURCE_INVALID,
            message=f"index store file absent: {path}",
        )
    try:
        rows = read_store(path)
    except (OSError, ValueError) as exc:
        return ForecastRefusal(
            code=FORECAST_SOURCE_INVALID,
            message=f"index store unreadable: {exc}",
        )
    series_sha = hashlib.sha256(path.read_bytes()).hexdigest()

    sessions: list[date] = []
    closes: list[float] = []
    non_session: list[str] = []
    out_of_scope = 0
    authority = _load_session_authority()
    for row in rows:
        try:
            d = date.fromisoformat(row[0])
            close = float(row[4]) if row[4] else float("nan")
        except ValueError as exc:
            return ForecastRefusal(
                code=FORECAST_SOURCE_INVALID,
                message=f"index row not parsable ({row[0]!r}): {exc}",
            )
        if not (close == close and close > 0.0):  # NaN or non-positive
            return ForecastRefusal(
                code=FORECAST_SOURCE_INVALID,
                message=(
                    f"index row {row[0]} has a non-finite or non-positive "
                    f"close ({row[4]!r}) — refusing rather than skipping "
                    f"a row silently"),
            )
        if not (SCOPE_START <= d <= SCOPE_END):
            out_of_scope += 1
            continue
        iso = d.isoformat()
        if iso not in authority:
            non_session.append(iso)
            continue
        sessions.append(d)
        closes.append(close)
    bad = _validate_grid(sessions, closes)
    if bad is not None:
        return ForecastRefusal(code=FORECAST_SOURCE_INVALID, message=bad)

    prov = _latest_provenance(root, name)
    if isinstance(prov, ForecastRefusal):
        return prov

    descriptor = SOURCE_REGISTRY[ForecastSourceId.INDEX_VIX]
    excluded: dict[str, tuple[str, ...]] = {}
    if non_session:
        excluded["non_session"] = tuple(non_session)
    provenance = dict(prov)
    provenance["out_of_scope_rows"] = out_of_scope
    return ForecastSeries(
        source_id=ForecastSourceId.INDEX_VIX,
        sessions=tuple(sessions),
        closes=tuple(closes),
        series_sha256=series_sha,
        basis=descriptor.basis,
        grid_basis=descriptor.grid_basis,
        provenance=provenance,
        excluded_rows=excluded,
        n_source_rows=len(rows),
    )


def _validate_grid(sessions: list[date],
                   closes: list[float]) -> str | None:
    """Shared grid invariants; a message when violated, None when ok."""
    for a, b in itertools.pairwise(sessions):
        if b <= a:
            return (f"sessions not strictly increasing: "
                    f"{a.isoformat()} -> {b.isoformat()}")
    for v in closes:
        if v != v or v <= 0.0:  # NaN or non-positive
            return f"non-finite or non-positive close: {v!r}"
    return None


def _load_session_authority() -> frozenset[str]:
    doc = json.loads(_SESSION_AUTHORITY.read_text())
    return frozenset(str(s) for s in doc["sessions"])


def _latest_provenance(
    root: Path, name: str,
) -> Mapping[str, Any] | ForecastRefusal:
    """The latest SUCCESSFUL provenance line for THIS source. The last
    line of the file can be another instrument (verified live: the file
    ends with a DTB3 entry)."""
    prov_path = root / "provenance.jsonl"
    if not prov_path.is_file():
        return ForecastRefusal(
            code=FORECAST_SOURCE_DRIFT,
            message=f"provenance.jsonl absent under {root}; cannot bind "
                    f"vendor provenance for {name}",
        )
    latest: dict[str, Any] | None = None
    for line in prov_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("source") == name and \
                entry.get("status") in _SUCCESS_STATUSES:
            latest = entry
    if latest is None:
        return ForecastRefusal(
            code=FORECAST_SOURCE_DRIFT,
            message=f"no successful provenance record for source {name}",
        )
    return {
        "kind": "desk-store",
        "fetched_at": latest.get("fetched_at"),
        "vendor_body_sha256": latest.get("sha256"),
        "status": latest.get("status"),
        "vendor_rows": latest.get("rows"),
        "vendor_last_date": latest.get("last_date"),
        "session_authority_sha256": hashlib.sha256(
            _SESSION_AUTHORITY.read_bytes()).hexdigest(),
        "sha_note": "vendor_body_sha256 hashes the downloaded vendor "
                    "body; series_sha256 hashes the stored CSV bytes — "
                    "they differ by design and both are recorded",
    }


__all__ = [
    "INTERVAL_SEMANTICS",
    "SCOPE_END",
    "SCOPE_START",
    "SOURCE_REGISTRY",
    "ForecastSeries",
    "SourceDescriptor",
    "load_index",
    "load_synthetic",
]
