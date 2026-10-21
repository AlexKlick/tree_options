"""tests/conftest.py keeps every test off the operator's host state.

The autouse isolation gives each test a private HOME plus XDG config/state/
data/cache dirs and drops every TREX_*/DESK_* variable; ``host_state`` opts a
test out. Oracles are plain environment facts, never the fixture's own code.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

PROBES = ("TREX_CONFTEST_PROBE", "DESK_CONFTEST_PROBE")
BYSTANDER = "CONFTEST_BYSTANDER_PROBE"  # no TREX_/DESK_ prefix: must survive
XDG_VARS = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")

_HOMES_SEEN: set[str] = set()


@pytest.fixture(scope="module")
def planted_host_env() -> Iterator[str]:
    """Plant variables the way an operator shell would. A module-scoped fixture
    is set up before the function-scoped autouse isolation, so these are in
    os.environ when the isolation starts. Yields the HOME the test run began with."""
    original_home = os.environ.get("HOME", "")
    with pytest.MonkeyPatch.context() as mp:
        for name in (*PROBES, BYSTANDER):
            mp.setenv(name, "planted")
        yield original_home


def _claim_fresh_home() -> Path:
    home = os.environ["HOME"]
    assert home not in _HOMES_SEEN, "two tests shared one HOME"
    _HOMES_SEEN.add(home)
    return Path(home)


def test_home_and_xdg_dirs_are_private_tmp_dirs(
    planted_host_env: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    home = _claim_fresh_home()
    basetemp = tmp_path_factory.getbasetemp()
    assert str(home) != planted_host_env
    assert home.is_dir() and home.is_relative_to(basetemp)
    assert Path.home() == home
    assert Path("~/.config").expanduser() == home / ".config"
    xdg = [Path(os.environ[var]) for var in XDG_VARS]
    assert len(set(xdg)) == len(XDG_VARS)
    for path in xdg:
        assert path.is_dir() and path.is_relative_to(basetemp)
        assert not os.listdir(path)  # fresh: nothing carried over


def test_trex_and_desk_variables_are_dropped(planted_host_env: str) -> None:
    _claim_fresh_home()
    assert not [name for name in os.environ if name.startswith(("TREX_", "DESK_"))]
    assert os.environ[BYSTANDER] == "planted"


@pytest.mark.host_state
def test_host_state_marker_opts_out(planted_host_env: str) -> None:
    assert os.environ.get("HOME", "") == planted_host_env
    for name in PROBES:
        assert os.environ[name] == "planted"


def test_host_environ_is_the_untouched_launch_environment(
    planted_host_env: str, host_environ: Mapping[str, str]
) -> None:
    assert host_environ.get("HOME", "") == planted_host_env
    assert host_environ.get("HOME", "") != os.environ["HOME"]
    with pytest.raises(TypeError):
        host_environ["HOME"] = "/elsewhere"  # type: ignore[index]
