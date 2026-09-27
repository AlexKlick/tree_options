"""Forecast sources (RL-3) — registry policy, fixture integrity, and
the session-authority contract over the frozen VIX slice.

The frozen slice (``data/research/fixtures/vix-slice-v1.csv``) carries
verbatim vendor rows for 2025-01-09 (NYSE Carter-day closure) and
2025-01-20 (MLK) — dates the repo's closure-corrected session authority
treats as NON-sessions — so the intersection + counted-exclusion
behavior is exercised hermetically, not against the moving live store.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import shutil
from pathlib import Path

import pytest

from tree_options.research.forecast.contracts import ForecastSourceId
from tree_options.research.forecast.refusal_codes import (
    FORECAST_SOURCE_DRIFT,
    FORECAST_SOURCE_INVALID,
)
from tree_options.research.forecast.sources import (
    INTERVAL_SEMANTICS,
    SCOPE_END,
    SCOPE_START,
    SOURCE_REGISTRY,
    ForecastSeries,
    load_index,
    load_synthetic,
    session_authority_sha256,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "data" / "research" / "fixtures"
SLICE = FIXTURES / "vix-slice-v1.csv"


def _series_or_fail(out: object) -> ForecastSeries:
    assert isinstance(out, ForecastSeries), out
    return out


class TestSynthetic:
    def test_loads_sha_pinned(self) -> None:
        out = _series_or_fail(load_synthetic())
        assert out.source_id is ForecastSourceId.SYNTHETIC
        body = (FIXTURES / "synthetic-forecast-v1.json").read_bytes()
        assert out.series_sha256 == hashlib.sha256(body).hexdigest()
        assert out.series_sha256 == (
            FIXTURES / "synthetic-forecast-v1.json.sha256").read_text().strip()

    def test_grid_invariants(self) -> None:
        out = _series_or_fail(load_synthetic())
        # 750 declared sessions (the fixture's documented size, derived
        # from the generator contract: first 750 pinned-calendar
        # sessions), strictly increasing, all closes positive.
        assert len(out.sessions) == 750
        assert all(b > a
                   for a, b in itertools.pairwise(out.sessions))
        assert all(c > 0.0 for c in out.closes)
        assert len(out.sessions) == len(out.closes)

    def test_tampered_fixture_is_drift(self, tmp_path: Path) -> None:
        shutil.copy(FIXTURES / "synthetic-forecast-v1.json",
                    tmp_path / "synthetic-forecast-v1.json")
        sidecar = tmp_path / "synthetic-forecast-v1.json.sha256"
        sidecar.write_text(
            (FIXTURES / "synthetic-forecast-v1.json.sha256").read_text())
        body = (tmp_path / "synthetic-forecast-v1.json").read_text()
        (tmp_path / "synthetic-forecast-v1.json").write_text(
            body.replace("100.0", "999.0", 1))
        out = load_synthetic(fixtures_dir=tmp_path)
        assert not isinstance(out, ForecastSeries)
        assert out.code == FORECAST_SOURCE_DRIFT
        # BOTH shas published — the refusal is its own evidence.
        assert "sidecar_sha256" in out.payload
        assert "file_sha256" in out.payload
        assert out.payload["sidecar_sha256"] != out.payload["file_sha256"]

    def test_absent_fixture_is_invalid(self, tmp_path: Path) -> None:
        out = load_synthetic(fixtures_dir=tmp_path)
        assert not isinstance(out, ForecastSeries)
        assert out.code == FORECAST_SOURCE_INVALID


class TestRegistryPolicy:
    def test_synthetic_enables_only_h5(self) -> None:
        d = SOURCE_REGISTRY[ForecastSourceId.SYNTHETIC]
        assert d.enabled_horizons == (5,)
        assert d.listed_horizons == (5,)

    def test_vix_enables_5_and_20_lists_four(self) -> None:
        d = SOURCE_REGISTRY[ForecastSourceId.INDEX_VIX]
        assert d.enabled_horizons == (5, 20)
        assert d.listed_horizons == (5, 20, 63, 126)

    def test_enabled_is_a_subset_of_listed(self) -> None:
        for d in SOURCE_REGISTRY.values():
            assert set(d.enabled_horizons) <= set(d.listed_horizons)

    def test_interval_semantics_copy_never_claims_calibration(self) -> None:
        low = INTERVAL_SEMANTICS.lower()
        # The CLAIM word must never appear; the concept may appear only
        # inside an explicit negation.
        assert "calibrated" not in low
        assert "not a claim of calibration" in low
        assert "quantile" in low
        assert "prediction interval" in low
        assert "not a confidence interval" in low


class TestIndexLoader:
    def _store(self, tmp_path: Path) -> Path:
        root = tmp_path / "indices"
        root.mkdir()
        shutil.copy(SLICE, root / "VIX.csv")
        # provenance: a VIX line followed by a DTB3 line — the last
        # line is ANOTHER instrument (the live file ends with DTB3; the
        # lookup must filter by source).
        vix_line = ('{"source": "VIX", "status": "updated", '
                    '"fetched_at": "2026-09-26T06:40:00-04:00", '
                    '"sha256": "' + "a" * 64 + '", "rows": 9281, '
                    '"last_date": "2026-09-25"}')
        dtb3_line = ('{"source": "DTB3", "status": "updated", '
                     '"fetched_at": "2026-09-26T06:40:20-04:00", '
                     '"sha256": "' + "b" * 64 + '", "rows": 18974, '
                     '"last_date": "2026-09-24"}')
        (root / "provenance.jsonl").write_text(
            vix_line + "\n" + dtb3_line + "\n")
        return root

    def test_loads_with_counted_closure_exclusions(self,
                                                   tmp_path: Path) -> None:
        out = _series_or_fail(load_index("VIX", store_root=self._store(tmp_path)))
        # The vendor file publishes a row on EVERY weekday — US market
        # holidays included (verified from the slice bytes: Memorial Day
        # 2024-05-27, Juneteenth, July 4th, Labor Day, Thanksgiving, and
        # the declared 2025-01-09 Carter closure). The expected exclusion
        # list is therefore DERIVED slice-date-by-date against the repo's
        # session authority, never restated by hand.
        slice_dates = [line.split(",")[0]
                       for line in SLICE.read_text().splitlines()[1:]]
        authority = set(json.loads(
            (REPO / "data" / "calendar" / "trex"
             / "nyse_sessions_2018_01_02_2028_12_29.json").read_text()
        )["sessions"])
        expected = tuple(d for d in slice_dates if d not in authority)
        assert out.n_source_rows == 300
        assert out.excluded_rows["non_session"] == expected
        # The two DESIGN-named cases stay pinned among them:
        assert "2025-01-09" in expected   # NYSE Carter-day closure
        assert "2025-01-20" in expected   # MLK
        assert len(expected) >= 6         # the holidays are real too
        assert len(out.sessions) == len(slice_dates) - len(expected)
        assert set(out.excluded_rows["non_session"]).isdisjoint(
            {s.isoformat() for s in out.sessions})
        assert all(b > a for a, b in itertools.pairwise(out.sessions))

    def test_series_sha_is_the_stored_bytes(self, tmp_path: Path) -> None:
        root = self._store(tmp_path)
        out = _series_or_fail(load_index("VIX", store_root=root))
        assert out.series_sha256 == hashlib.sha256(
            (root / "VIX.csv").read_bytes()).hexdigest()

    def test_provenance_selects_this_source_not_the_last_line(
            self, tmp_path: Path) -> None:
        out = _series_or_fail(load_index("VIX", store_root=self._store(tmp_path)))
        assert out.provenance["vendor_body_sha256"] == "a" * 64
        assert out.provenance["status"] == "updated"
        assert "session_authority_sha256" in out.provenance
        assert "vendor_body_sha256" in out.provenance["sha_note"]
        assert "series_sha256" in out.provenance["sha_note"]

    def test_scope_drops_out_of_window_rows(self, tmp_path: Path) -> None:
        root = self._store(tmp_path)
        lines = (root / "VIX.csv").read_text().splitlines()
        # Insert a 2017 row at the head (stays strictly increasing) —
        # outside the declared [2018-01-02, 2026-12-31] scope.
        lines.insert(1, "2017-06-15,10.0,10.0,10.0,10.0")
        (root / "VIX.csv").write_text("\n".join(lines) + "\n")
        out = _series_or_fail(load_index("VIX", store_root=root))
        assert out.n_source_rows == 301
        # Derived identity: every source row is in-scope-and-session,
        # out-of-scope, or excluded as a non-session — nothing else.
        n_excluded = len(out.excluded_rows.get("non_session", ()))
        assert len(out.sessions) == 301 - 1 - n_excluded
        assert out.provenance["out_of_scope_rows"] == 1
        assert out.sessions[0].isoformat() >= SCOPE_START.isoformat()
        assert out.sessions[-1].isoformat() <= SCOPE_END.isoformat()

    def test_missing_provenance_refuses(self, tmp_path: Path) -> None:
        root = self._store(tmp_path)
        (root / "provenance.jsonl").unlink()
        out = load_index("VIX", store_root=root)
        assert not isinstance(out, ForecastSeries)
        assert out.code == FORECAST_SOURCE_DRIFT

    @pytest.mark.parametrize("bad_row", [
        "2025-03-03,10.0,10.0,10.0,-5.0",     # negative close
        "2025-03-03,10.0,10.0,10.0,",         # empty close
        # +inf parses, compares equal to itself, and is > 0 — a NaN-only
        # guard would accept it (checkpoint B, P2-4)
        "2025-03-03,10.0,10.0,10.0,inf",
        "2025-03-03,10.0,10.0,10.0,1e999",
    ])
    def test_bad_close_refuses_rather_than_skips(
            self, tmp_path: Path, bad_row: str) -> None:
        root = self._store(tmp_path)
        lines = (root / "VIX.csv").read_text().splitlines()
        lines.append(bad_row)
        (root / "VIX.csv").write_text("\n".join(lines) + "\n")
        out = load_index("VIX", store_root=root)
        assert not isinstance(out, ForecastSeries)
        assert out.code == FORECAST_SOURCE_INVALID
        assert "2025-03-03" in out.message

    def test_positive_infinity_level_refuses(self, tmp_path: Path) -> None:
        # json.loads accepts bare Infinity — the shared grid validator
        # must reject it (isfinite, not a NaN-only check; P2-4).
        doc = json.loads(
            (FIXTURES / "synthetic-forecast-v1.json").read_text())
        doc["levels"][10] = float("inf")
        d = tmp_path / "synth"
        d.mkdir()
        (d / "synthetic-forecast-v1.json").write_text(json.dumps(doc))
        out = load_synthetic(fixtures_dir=d)
        assert not isinstance(out, ForecastSeries)
        assert out.code == FORECAST_SOURCE_INVALID
        assert "non-finite" in out.message

    def test_session_authority_sha_is_the_authority_bytes(
            self, tmp_path: Path) -> None:
        # The calendar identity bound into run ids hashes exactly the
        # authority file's bytes — recomputed here from the file, not
        # from the implementation's constant.
        body = (REPO / "data" / "calendar" / "trex"
                / "nyse_sessions_2018_01_02_2028_12_29.json").read_bytes()
        assert session_authority_sha256() == \
            hashlib.sha256(body).hexdigest()
        # and the index lane's provenance binds the SAME identity
        out = _series_or_fail(
            load_index("VIX", store_root=self._store(tmp_path)))
        assert out.provenance["session_authority_sha256"] == \
            session_authority_sha256()

    def test_unreadable_authority_is_a_typed_refusal(
            self, tmp_path: Path,
            monkeypatch: pytest.MonkeyPatch) -> None:
        # Malformed authority bytes hash fine but do not parse: the
        # loader refuses source_invalid instead of raising into a
        # generic failed run (checkpoint B-prime, N2).
        import tree_options.research.forecast.sources as sources_mod
        bad = tmp_path / "authority.json"
        bad.write_text("{not json")
        monkeypatch.setattr(sources_mod, "_SESSION_AUTHORITY", bad)
        out = load_index("VIX", store_root=self._store(tmp_path))
        assert not isinstance(out, ForecastSeries)
        assert out.code == FORECAST_SOURCE_INVALID
        assert "authority unreadable" in out.message

    def test_duplicate_date_refuses(self, tmp_path: Path) -> None:
        root = self._store(tmp_path)
        lines = (root / "VIX.csv").read_text().splitlines()
        lines.append(lines[1])  # duplicate an existing row verbatim
        (root / "VIX.csv").write_text("\n".join(lines) + "\n")
        out = load_index("VIX", store_root=root)
        assert not isinstance(out, ForecastSeries)
        assert out.code == FORECAST_SOURCE_INVALID
        assert "strictly increasing" in out.message
