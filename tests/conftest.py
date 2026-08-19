"""Shared fixtures: protocol, calendars, hypothesis profiles."""

from __future__ import annotations

from datetime import date
from pathlib import Path

# Must run before the first numpy import anywhere in the suite: BLAS
# thread counts change reduction orders (M2-proper §3.D).
from tree_options.models.determinism import force_single_threaded_blas

force_single_threaded_blas()

import pytest  # noqa: E402 — after the BLAS pin above, on purpose
from hypothesis import settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_START = date(2019, 1, 7)  # a Monday
SYNTHETIC_SESSIONS = 1500

# Deterministic by default so a clean clone produces byte-identical counts.
# `thorough` (randomized, 1000 examples) is an exploration tool, never the gate.
settings.register_profile("default", max_examples=50, deadline=None, derandomize=True)
settings.register_profile("thorough", max_examples=1000, deadline=None)
settings.load_profile("default")


@pytest.fixture()
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture()
def protocol_path() -> Path:
    return REPO_ROOT / "research_protocol.yaml"


@pytest.fixture()
def protocol():
    from tree_options.protocol.loader import load_protocol

    return load_protocol()


@pytest.fixture()
def static_calendar():
    from tree_options.time.calendar import StaticSessionCalendar

    return StaticSessionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )


@pytest.fixture()
def synthetic_calendar():
    from tree_options.time.synthetic import SyntheticCalendar

    return SyntheticCalendar(start_date=SYNTHETIC_START, n_sessions=SYNTHETIC_SESSIONS)
