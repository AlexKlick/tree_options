"""Workstream A: OptionsOverlaySpec validation (M3 plan §3.A)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tree_options.synth_options.spec import OptionsOverlaySpec


def base_spec(**overrides: object) -> OptionsOverlaySpec:
    params: dict[str, object] = {"world_id": "spec-test-1", "seed": 1}
    params.update(overrides)
    return OptionsOverlaySpec(**params)  # type: ignore[arg-type]


def test_defaults_are_valid() -> None:
    spec = base_spec()
    assert spec.overlay_version == "v2"
    assert spec.eligible_top_n == 100
    assert spec.max_live_expiries == 10
    assert spec.weekly_listing_dte == 70
    assert spec.quarterly_listing_dte == 397
    assert spec.untraded_fraction == 0.5


def test_inverted_iv_base_range_rejected() -> None:
    with pytest.raises(ValidationError, match="iv_base_min"):
        base_spec(iv_base_min=0.5, iv_base_max=0.4)


def test_inverted_iv_multiplier_range_rejected() -> None:
    with pytest.raises(ValidationError, match="iv_expiry_mult_min"):
        base_spec(iv_expiry_mult_min=1.3, iv_expiry_mult_max=1.1)


def test_min_eligible_bars_above_window_rejected() -> None:
    with pytest.raises(ValidationError, match="min_eligible_bars"):
        base_spec(min_eligible_bars=30, eligibility_window_bars=20)


def test_quarterly_window_below_weekly_rejected() -> None:
    with pytest.raises(ValidationError, match="quarterly_listing_dte"):
        base_spec(quarterly_listing_dte=60, weekly_listing_dte=70)


@pytest.mark.parametrize(
    "field",
    [
        "eligible_top_n",
        "n_moneyness_nodes",
        "max_live_expiries",
        "moneyness_span",
        "untraded_fraction",
    ],
)
def test_bounds_enforced(field: str) -> None:
    low, high = {
        "eligible_top_n": (5, 10_000),
        "n_moneyness_nodes": (2, 102),
        "max_live_expiries": (1, 41),
        "moneyness_span": (0.01, 0.95),
        "untraded_fraction": (-0.1, 1.1),
    }[field]
    with pytest.raises(ValidationError):
        base_spec(**{field: low})
    with pytest.raises(ValidationError):
        base_spec(**{field: high})


def test_unknown_fields_are_defects() -> None:
    with pytest.raises(ValidationError, match="extra"):
        base_spec(surprise=True)
