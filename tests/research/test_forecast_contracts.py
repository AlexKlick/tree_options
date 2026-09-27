"""Forecast contracts (RL-3) — parse surface, execution-bound run id,
and the ONE literal wire pin of the campaign.

Every oracle below is derived from the spec semantics (what the fields
mean), never from an implementation expression — mirroring the
discipline of ``test_funded.py`` / ``test_scenario.py``. The single
literal (``test_spec_wire_literal_is_pinned_once``) is the campaign's
one allowed literal pin: the spec dict is the hashed request surface, so
pinning it pins the reproducibility of every forecast run id.
"""
from __future__ import annotations

from datetime import date

import pytest

from tree_options.research.forecast.contracts import (
    ORIGIN_FLOOR,
    PAIRED_FLOOR,
    QUANTILE_GRID,
    ForecastSourceId,
    ForecastSpec,
    forecast_run_id,
    tally_identity_ok,
)
from tree_options.research.forecast.spec_io import (
    FORECAST_SPEC_FIELDS,
    forecast_from_dict,
)


def _spec() -> ForecastSpec:
    return ForecastSpec(
        source=ForecastSourceId.INDEX_VIX,
        horizon=5,
        evaluation_start=date(2018, 2, 1),
        evaluation_end=None,
    )


class TestParse:
    def test_round_trip_through_the_wire(self) -> None:
        spec = _spec()
        parsed = forecast_from_dict(spec.to_dict())
        assert parsed == spec

    def test_evaluation_end_is_optional_and_parsed(self) -> None:
        spec = ForecastSpec(
            source=ForecastSourceId.SYNTHETIC,
            horizon=5,
            evaluation_start=date(2024, 1, 2),
            evaluation_end=date(2024, 6, 28),
        )
        assert forecast_from_dict(spec.to_dict()) == spec

    def test_non_object_body_refused(self) -> None:
        with pytest.raises(ValueError, match="must be a JSON object"):
            forecast_from_dict(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_unknown_field_refused_pre_write(self) -> None:
        # The spec is the hashed request: an unknown field is a different
        # request and must never silently alias an existing run id.
        with pytest.raises(ValueError, match="outside the spec surface"):
            forecast_from_dict({
                "source": "index:VIX", "horizon": 5,
                "evaluation_start": "2018-02-01", "models": ["ar1"],
            })

    @pytest.mark.parametrize("missing", ["source", "horizon",
                                         "evaluation_start"])
    def test_missing_required_field_refused(self, missing: str) -> None:
        body = {"source": "index:VIX", "horizon": 5,
                "evaluation_start": "2018-02-01"}
        del body[missing]
        with pytest.raises(ValueError, match=f"missing '{missing}'"):
            forecast_from_dict(body)

    def test_unknown_source_refused_with_the_known_list(self) -> None:
        with pytest.raises(ValueError, match="unknown forecast source"):
            forecast_from_dict({"source": "nope", "horizon": 5,
                                "evaluation_start": "2018-02-01"})

    @pytest.mark.parametrize("bad", [True, 5.0, "5", None])
    def test_non_integer_horizon_refused(self, bad: object) -> None:
        # bool is rejected even though isinstance(True, int): a wire
        # boolean is never a session count.
        with pytest.raises(ValueError, match="'horizon' must be"):
            forecast_from_dict({"source": "index:VIX", "horizon": bad,
                                "evaluation_start": "2018-02-01"})

    @pytest.mark.parametrize("bad", [0, -5])
    def test_non_positive_horizon_refused(self, bad: int) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            forecast_from_dict({"source": "index:VIX", "horizon": bad,
                                "evaluation_start": "2018-02-01"})

    def test_bad_iso_date_refused(self) -> None:
        with pytest.raises(ValueError, match="not an ISO date"):
            forecast_from_dict({"source": "index:VIX", "horizon": 5,
                                "evaluation_start": "Feb 1 2018"})

    def test_inverted_window_refused(self) -> None:
        with pytest.raises(ValueError, match="precedes"):
            forecast_from_dict({
                "source": "index:VIX", "horizon": 5,
                "evaluation_start": "2018-06-01",
                "evaluation_end": "2018-02-01",
            })

    def test_spec_fields_surface_is_frozen(self) -> None:
        # The wire surface names exactly the spec dataclass fields with
        # defaults — derived from the dataclass, not restated by hand.
        import dataclasses
        names = {f.name for f in dataclasses.fields(ForecastSpec)}
        assert set(FORECAST_SPEC_FIELDS) == names


class TestRunId:
    """Execution-bound identity: spec + series + BOTH calendars +
    engine. Any of the five changing produces a NEW run — derived from
    the hash binding contract, checked by mutation."""

    def _rid(self, **overrides: str) -> str:
        base = dict(series_sha256="s" * 64, calendar_sha256="c" * 64,
                    session_authority_sha256="a" * 64,
                    engine_sha256="e" * 64)
        return forecast_run_id(_spec(), **{**base, **overrides})

    def test_identical_inputs_same_id(self) -> None:
        assert self._rid() == self._rid()

    def test_any_identity_change_is_a_new_run(self) -> None:
        ref = self._rid()
        # spec change
        changed_spec = ForecastSpec(
            source=ForecastSourceId.INDEX_VIX, horizon=20,
            evaluation_start=date(2018, 2, 1))
        assert forecast_run_id(
            changed_spec, series_sha256="s" * 64, calendar_sha256="c" * 64,
            session_authority_sha256="a" * 64,
            engine_sha256="e" * 64) != ref
        # data revision
        assert self._rid(series_sha256="t" * 64) != ref
        # comparison-calendar change (declared scope)
        assert self._rid(calendar_sha256="d" * 64) != ref
        # session-authority change: a closure correction re-grades the
        # grid (targets and scores move) — checkpoint B, P1-1
        assert self._rid(session_authority_sha256="b" * 64) != ref
        # engine change (an engine fix can never re-serve a stale receipt)
        assert self._rid(engine_sha256="f" * 64) != ref

    def test_run_id_is_64_hex(self) -> None:
        rid = self._rid()
        assert len(rid) == 64 and all(c in "0123456789abcdef" for c in rid)


class TestConstants:
    def test_quantile_grid_is_a_valid_grid(self) -> None:
        # Derived properties: strictly increasing, interior probabilities.
        assert all(0.0 < t < 1.0 for t in QUANTILE_GRID)
        assert list(QUANTILE_GRID) == sorted(QUANTILE_GRID)
        assert len(set(QUANTILE_GRID)) == len(QUANTILE_GRID)

    def test_floors_are_coherent(self) -> None:
        # The paired floor is meaningful only at or below the origin
        # floor (a paired cohort can never exceed the evaluated count).
        assert 0 < PAIRED_FLOOR <= ORIGIN_FLOOR

    def test_tally_identity(self) -> None:
        assert tally_identity_ok(total=10, evaluated=7, excluded=2, failed=1)
        assert not tally_identity_ok(total=10, evaluated=7, excluded=2,
                                     failed=2)


def test_spec_wire_literal_is_pinned_once() -> None:
    # The campaign's ONE literal pin: this exact dict is the hashed
    # request surface; every forecast run id derives from it. A change
    # here is a wire-format change and must be deliberate.
    assert _spec().to_dict() == {
        "source": "index:VIX",
        "horizon": 5,
        "evaluation_start": "2018-02-01",
        "evaluation_end": None,
        "proposed_by": "operator",
        "notes": "",
    }
