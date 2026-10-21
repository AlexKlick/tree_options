"""Release identity and scheduler readiness are independent of trade authority."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex import premarket, release


@pytest.fixture
def release_config(tmp_path, monkeypatch):
    source = tmp_path / "source"
    static = source / "src/tree_options/trex_web/static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("frozen assets")
    (source / "uv.lock").write_text("frozen python")
    (source / "web").mkdir()
    (source / "web/package-lock.json").write_text("frozen frontend")
    config = {
        "source": str(source),
        "orders_authorized": False,
        "live_money": False,
        "previous_release": None,
        "first_session": "2026-10-01",
    }
    config.update({key: str(tmp_path / "state" / key) for key in release.STATE_KEYS})
    Path(config["experiment_sources"]).parent.mkdir(parents=True, exist_ok=True)
    Path(config["experiment_sources"]).write_text(
        '{"schema":"trex-experiment-sources/1","sources":{}}'
    )
    config["experiment_snapshot"] = str(tmp_path / "state/desk_state/read-model/experiments.json")
    config["read_snapshot"] = str(tmp_path / "state/desk_state/read-model/evidence.json")
    monkeypatch.setattr(
        release, "_git", lambda path, *args: "" if args[0] == "status" else "a" * 40
    )
    monkeypatch.setattr(release, "_environment", lambda path: {"python": "frozen", "packages": []})
    return config


@pytest.mark.parametrize(
    "change",
    ["asset", "extra_asset", "python_lock", "head", "packages", "authority", "experiment_sources"],
)
def test_sealed_release_refuses_drift(release_config, monkeypatch, change):
    manifest = release.seal(release_config)
    source = Path(manifest["source"])
    release.verify_manifest(manifest)
    if change == "experiment_sources":
        Path(manifest["experiment_sources"]).write_text("changed sources")
    elif change == "asset":
        (source / "src/tree_options/trex_web/static/index.html").write_text("changed")
    elif change == "extra_asset":
        (source / "src/tree_options/trex_web/static/unbound.js").write_text("changed")
    elif change == "python_lock":
        (source / "uv.lock").write_text("changed")
    elif change == "head":
        monkeypatch.setattr(
            release, "_git", lambda path, *args: "" if args[0] == "status" else "b" * 40
        )
    elif change == "packages":
        monkeypatch.setattr(release, "_environment", lambda path: {"python": "other"})
    else:
        manifest["orders_authorized"] = True
    with pytest.raises(ValueError):
        release.verify_manifest(manifest)


def test_release_refuses_state_in_replaceable_source(release_config):
    release_config["paper_workspace"] = release_config["source"] + "/state"
    with pytest.raises(ValueError, match="release_state_inside_source"):
        release.seal(release_config)


@pytest.mark.parametrize("check_status,exit_code", [("FAIL", 3), ("BLOCKED", 3), ("PASS", 0)])
def test_premarket_cannot_hide_failed_prerequisite(tmp_path, monkeypatch, check_status, exit_code):
    config = {"source": str(tmp_path), "release_head": "a" * 40, "first_session": "2026-10-01"}
    monkeypatch.setattr(premarket, "config", lambda: config)
    monkeypatch.setattr(premarket, "verify", lambda config: tmp_path)
    monkeypatch.setattr(premarket, "window", lambda *args: None)
    monkeypatch.setattr(
        premarket, "workspace_summary", lambda config: {"broker": {"status": "BLOCKED"}}
    )
    monkeypatch.setattr(premarket, "shadow_input_check", lambda *args: {"status": check_status})
    monkeypatch.setattr(premarket, "activate_workspace", lambda source: {"status": "PASS"})
    monkeypatch.setattr(premarket, "RUNS", tmp_path / "runs")
    assert premarket.main(["--run"]) == exit_code
    result = json.loads((tmp_path / "runs/latest.json").read_text())
    assert result["status"] == (
        "RESEARCH_STARTED_BROKER_BLOCKED" if check_status == "PASS" else "RESEARCH_INPUTS_BLOCKED"
    )
    assert result["orders_authorized"] is False
    assert result["broker"]["status"] == "BLOCKED"


@pytest.mark.parametrize(
    "day,hour,expected",
    [
        ("2026-11-02", 8, None),  # first weekday after fall DST change
        ("2026-11-02", 10, "SKIPPED_OUTSIDE_PREMARKET_WINDOW"),
        ("2026-11-01", 8, "SKIPPED_NON_SESSION"),
        ("2026-11-26", 8, "SKIPPED_NON_SESSION"),  # Thanksgiving
    ],
)
def test_premarket_nyse_window(day, hour, expected):
    from tree_options.trex.clock import session_calendar

    now = datetime.fromisoformat(day).replace(hour=hour, tzinfo=ZoneInfo("America/New_York"))
    assert premarket.window(now, {"first_session": "2026-10-01"}, session_calendar()) == expected


def test_release_refuses_snapshot_outside_publisher_write_root(release_config):
    release_config["read_snapshot"] = release_config["source"] + "-elsewhere/snapshot.json"
    with pytest.raises(ValueError, match="snapshot_outside_publisher_state"):
        release.seal(release_config)


# --- Rendered units: every release-pinned unit, pinned to one release -------

REPO_TEMPLATES = Path(__file__).resolve().parents[2] / "deploy/product"
# Live units that today run outside the serving checkout (a release worktree or
# an untracked copy). Each must get a drop-in pinned to the rendered release.
RELEASE_DROPINS = {
    "desk-qsl",
    "trex-web",
    "trex-web-staging",
    "desk-evidence",
    "desk-admission-preview",
}
EXEC_KEYS = ("ExecStartPre", "ExecStart", "ExecStartPost")


def _directives(text):
    """(key, value) pairs of a unit file, comments and section headers dropped."""
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        pairs.append((key, value))
    return pairs


@pytest.fixture
def rendered(release_config, tmp_path):
    source = Path(release_config["source"])
    (source / "deploy/product").mkdir(parents=True)
    for template in REPO_TEMPLATES.glob("*.in"):
        (source / "deploy/product" / template.name).write_text(template.read_text())
    manifest = release.seal(release_config)
    units = tmp_path / "release/units"
    release.render_units(manifest, units)
    return manifest, units


def test_render_covers_every_release_pinned_unit(rendered):
    _, units = rendered
    dropins = {p.name.removesuffix("-release.conf") for p in units.glob("*-release.conf")}
    assert dropins == RELEASE_DROPINS


def test_every_dropin_pins_code_to_the_release_and_gates_on_its_identity(rendered):
    manifest, units = rendered
    source = manifest["source"]
    release_dir = str(units.parent)
    for name in RELEASE_DROPINS:
        pairs = _directives((units / f"{name}-release.conf").read_text())
        assert ("WorkingDirectory", source) in pairs, name
        assert ("Environment", f"PYTHONPATH={source}/src") in pairs, name
        assert ("Environment", f"TREX_RELEASE_DIR={release_dir}") in pairs, name
        for key in EXEC_KEYS:
            values = [v for k, v in pairs if k == key]
            # A drop-in appends to the base unit's command list unless it resets
            # it first; a oneshot would then run the OLD release, then this one.
            assert values and values[0] == "", f"{name}: {key} not reset first"
            for value in values[1:]:
                assert value.split()[0] == f"{source}/.venv/bin/python", (name, key, value)
        gates = [v for k, v in pairs if k == "ExecStartPre" and v]
        assert gates == [f"{source}/.venv/bin/python -m tree_options.trex.premarket --check"], name


def test_staging_dropin_never_points_at_release_state(rendered):
    manifest, units = rendered
    text = (units / "trex-web-staging-release.conf").read_text()
    pairs = _directives(text)
    for key in release.STATE_KEYS:
        assert manifest[key] not in text, key
    assert not [k for k, _ in pairs if k == "ReadWritePaths"]
    starts = [v for k, v in pairs if k == "ExecStart" and v]
    assert starts == [
        f"{manifest['source']}/.venv/bin/python -m tree_options.trex_web --host 127.0.0.1 --port 8091"
    ]


def test_desk_dropins_write_only_inside_desk_state(rendered):
    manifest, units = rendered
    desk_state = Path(manifest["desk_state"])
    for unit in units.iterdir():
        for key, value in _directives(unit.read_text()):
            if key == "ReadWritePaths":
                for path in value.split():
                    assert Path(path).is_relative_to(desk_state), (unit.name, path)


def test_sandboxed_units_never_launch_through_host_work(rendered):
    # host-work writes a payload under /run/user and starts a transient unit:
    # under ProtectHome= that write fails, and if it did not, the workload would
    # run outside the unit's sandbox. Drop-ins inherit a sandboxed base unit.
    _, units = rendered
    for unit in units.iterdir():
        pairs = _directives(unit.read_text())
        sandboxed = unit.suffix == ".conf" or any(k == "ProtectHome" for k, _ in pairs)
        if not sandboxed:
            continue
        for key, value in pairs:
            if key in EXEC_KEYS and value:
                assert "host-test" not in value and "host-work" not in value, (unit.name, value)


def test_no_rendered_unit_arms_entries(rendered):
    _, units = rendered
    for unit in units.iterdir():
        for key, value in _directives(unit.read_text()):
            assert "--armed" not in value.split(), (unit.name, key, value)


def test_render_refuses_template_command_outside_release_commands(release_config, tmp_path):
    source = Path(release_config["source"])
    (source / "deploy/product").mkdir(parents=True)
    (source / "deploy/product/rogue-release.conf.in").write_text(
        "[Service]\nExecStart=\nExecStart=@SOURCE@/.venv/bin/python -m tree_options.desk desk-arm\n"
    )
    manifest = release.seal(release_config)
    with pytest.raises(ValueError, match="release_template_command_unbound"):
        release.render_units(manifest, tmp_path / "release/units")
    # All or nothing: a refused render leaves no partial unit set to install.
    assert not (tmp_path / "release/units").exists()


def test_render_is_pure(rendered, release_config, tmp_path, monkeypatch):
    manifest, _ = rendered

    def forbidden(*args, **kwargs):
        raise AssertionError(f"render ran a process: {args}")

    for name in ("run", "check_output", "check_call", "call", "Popen"):
        monkeypatch.setattr(release.subprocess, name, forbidden)
    release.render_units(manifest, tmp_path / "again/units")
    assert sorted(p.name for p in (tmp_path / "again/units").iterdir())
