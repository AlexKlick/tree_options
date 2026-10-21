"""Shared fixtures: host isolation, protocol, calendars, hypothesis profiles."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from types import MappingProxyType

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

# The environment the run was launched with, captured before any test's
# isolation: the explicit, read-only way to reach an operator-supplied knob
# (e.g. TREX_V1_PIN_BUNDLE) that the isolation below scrubs.
HOST_ENVIRON: Mapping[str, str] = MappingProxyType(dict(os.environ))

HOST_STATE_MARKER = "host_state"
_SCRUBBED_ENV_PREFIXES = ("TREX_", "DESK_")
_XDG_DIRS = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_STATE_HOME": ".local/state",
    "XDG_DATA_HOME": ".local/share",
    "XDG_CACHE_HOME": ".cache",
}


@pytest.fixture(autouse=True)
def _isolate_host_state(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Keep every test off the operator's live host state.

    Each test gets a fresh HOME under the pytest basetemp, with
    XDG_CONFIG_HOME / XDG_STATE_HOME / XDG_DATA_HOME / XDG_CACHE_HOME as
    empty dirs inside it, and runs with every TREX_* and DESK_* variable
    removed (they point the code at live state roots, configs and stores).
    That redirects CALL-TIME lookups only: Path.home(), expanduser() and
    os.environ reads made while the test runs, including in subprocesses it
    spawns.

    It does NOT redirect module-level constants that src computed from HOME
    at import time (e.g. trex_web.app.DEFAULT_STATE_ROOT,
    trex.notify.DEFAULT_CONFIG): test modules are imported during collection,
    before any fixture runs, so those still name the real home. A test that
    reaches one must pass an explicit path or monkeypatch the attribute.

    ``@pytest.mark.host_state`` opts a test out: it then sees the launch
    environment unchanged, and should still read the real home explicitly
    (pwd.getpwuid(os.getuid()).pw_dir) rather than through HOME.
    """
    if request.node.get_closest_marker(HOST_STATE_MARKER) is not None:
        return
    for name in list(os.environ):
        if name.startswith(_SCRUBBED_ENV_PREFIXES):
            monkeypatch.delenv(name)
    # mkdtemp, not tmp_path: tests that list their tmp_path must see it
    # empty, and a numbered mktemp rescans the basetemp on every call.
    home = Path(tempfile.mkdtemp(prefix="home-", dir=tmp_path_factory.getbasetemp()))
    monkeypatch.setenv("HOME", str(home))
    for var, rel in _XDG_DIRS.items():
        path = home / rel
        path.mkdir(parents=True)
        monkeypatch.setenv(var, str(path))


@pytest.fixture(scope="session")
def host_environ() -> Mapping[str, str]:
    """Read-only view of the launch environment (see HOST_ENVIRON)."""
    return HOST_ENVIRON


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
