"""Release identity and scheduler readiness are independent of trade authority."""

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex import premarket, release

REAL_GIT = release._git
REAL_DETACHED = getattr(release, "_detached", None)


def _populate(source):
    """The frozen files a release seal hashes."""
    static = source / "src/tree_options/trex_web/static"
    static.mkdir(parents=True)
    (static / "index.html").write_text("frozen assets")
    (source / "uv.lock").write_text("frozen python")
    (source / "web").mkdir()
    (source / "web/package-lock.json").write_text("frozen frontend")


def _config(tmp_path, source):
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
    return config


def _fake_git(main, head):
    """A clean linked worktree whose main worktree is `main`, at `head`."""

    def git(path, *args):
        if args[0] == "status":
            return ""
        if "--git-common-dir" in args:
            return str(main / ".git")
        return head

    return git


@pytest.fixture
def release_config(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _populate(source)
    config = _config(tmp_path, source)
    monkeypatch.setattr(release, "_git", _fake_git(tmp_path / "main", "a" * 40))
    monkeypatch.setattr(release, "_detached", lambda path: True, raising=False)
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
        monkeypatch.setattr(release, "_git", _fake_git(source.parent / "main", "b" * 40))
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


# --- Source identity: a detached linked worktree, never a serving checkout ----


def _git_in(cwd, *args):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=release-test",
            "-c",
            "user.email=release-test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "init.defaultBranch=main",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_layout(tmp_path, monkeypatch):
    """A real repository: main worktree on a branch plus three linked worktrees."""
    monkeypatch.setattr(release, "_environment", lambda path: {"python": "frozen", "packages": []})
    main = tmp_path / "main"
    main.mkdir()
    _git_in(main, "init", "-q")
    _populate(main)
    (main / ".git/info/exclude").write_text("inner/\n")
    _git_in(main, "add", "-A")
    _git_in(main, "commit", "-qm", "candidate")
    layout = {
        "main": main,
        "detached": tmp_path / "releases/candidate/source",
        "branch": tmp_path / "lane",
        "inner": main / "inner",
    }
    _git_in(main, "worktree", "add", "-q", "--detach", str(layout["detached"]), "HEAD")
    _git_in(main, "worktree", "add", "-q", "-b", "lane", str(layout["branch"]))
    _git_in(main, "worktree", "add", "-q", "--detach", str(layout["inner"]), "HEAD")
    return layout


def test_release_seals_a_detached_linked_worktree(git_layout, tmp_path):
    source = git_layout["detached"]
    manifest = release.seal(_config(tmp_path, source))
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    assert manifest["release_head"] == head


@pytest.mark.parametrize(
    "which,reason",
    [
        ("branch", "release_source_not_detached"),
        ("main", "release_source_not_detached"),
        ("inner", "release_source_in_main_worktree"),
    ],
)
def test_release_refuses_branch_or_main_worktree_source(git_layout, tmp_path, which, reason):
    with pytest.raises(ValueError, match=reason):
        release.seal(_config(tmp_path, git_layout[which]))


def test_release_refuses_detached_main_worktree(git_layout, tmp_path):
    # Detaching the serving checkout does not make it a release.
    _git_in(git_layout["main"], "checkout", "-q", "--detach")
    with pytest.raises(ValueError, match="release_source_in_main_worktree"):
        release.seal(_config(tmp_path, git_layout["main"]))


# --- previous_release: hashed at seal, never a live dependency ---------------


def test_previous_release_is_hashed_at_seal_and_not_required_at_verify(release_config, tmp_path):
    previous = tmp_path / "releases/older/config.json"
    previous.parent.mkdir(parents=True)
    previous.write_text('{"schema": "trex-research-release/2", "release_head": "older"}\n')
    expected = hashlib.sha256(previous.read_bytes()).hexdigest()
    release_config["previous_release"] = str(previous)
    manifest = release.seal(release_config)
    assert manifest["previous_release_sha256"] == expected
    previous.unlink()
    previous.parent.rmdir()
    # A live gate must survive pruning the previous release directory.
    release.verify_manifest(manifest)


def test_seal_refuses_missing_previous_release(release_config, tmp_path):
    release_config["previous_release"] = str(tmp_path / "releases/gone/config.json")
    with pytest.raises(ValueError, match="previous_release_manifest_missing"):
        release.seal(release_config)


@pytest.mark.parametrize("recorded", ["absent", None, "", "0" * 63, "G" * 64, "pair"])
def test_verify_refuses_previous_release_without_its_sealed_hash(
    release_config, tmp_path, recorded
):
    previous = tmp_path / "older.json"
    previous.write_text("{}\n")
    release_config["previous_release"] = str(previous)
    manifest = release.seal(release_config)
    if recorded == "absent":
        del manifest["previous_release_sha256"]
    elif recorded == "pair":
        # A hash with no previous release is as inconsistent as the reverse.
        manifest["previous_release"] = None
    else:
        manifest["previous_release_sha256"] = recorded
    with pytest.raises(ValueError, match="previous_release_hash_invalid"):
        release.verify_manifest(manifest)


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
    "where,reason",
    [
        ("foreign_code", "premarket_code_outside_release"),
        ("foreign_interpreter", "premarket_interpreter_outside_release"),
        ("release", None),
    ],
)
def test_premarket_check_binds_its_own_code_and_interpreter(tmp_path, monkeypatch, where, reason):
    # A config.json can name any source; the gate must be that source's own code.
    source = tmp_path / "release/source"
    verified = []
    monkeypatch.setattr(release, "verify_manifest", verified.append)
    code = source / "src/tree_options/trex/premarket.py"
    prefix = source / ".venv"
    if where == "foreign_code":
        code = tmp_path / "serving/src/tree_options/trex/premarket.py"
    elif where == "foreign_interpreter":
        prefix = tmp_path / "serving/.venv"
    monkeypatch.setattr(premarket, "__file__", str(code))
    monkeypatch.setattr(premarket.sys, "prefix", str(prefix))
    c = {"source": str(source)}
    if reason is None:
        assert premarket.verify(c) == source
        assert verified == [c]
    else:
        with pytest.raises(ValueError, match=reason):
            premarket.verify(c)


def test_premarket_check_run_from_another_tree_fails_closed(tmp_path, monkeypatch):
    # This test process is not the release named in the config: --check refuses.
    config = {"source": str(tmp_path / "release/source"), "release_head": "a" * 40}
    monkeypatch.setattr(premarket, "config", lambda: config)
    monkeypatch.setattr(release, "verify_manifest", lambda manifest: None)
    monkeypatch.setattr(premarket, "RUNS", tmp_path / "runs")
    assert premarket.main(["--check"]) == 1
    report = json.loads((tmp_path / "runs/latest.json").read_text())
    assert (report["mode"], report["status"]) == ("check", "FAILED")
    assert report["reason"] == "premarket_code_outside_release"


@pytest.fixture
def read_only_runs(tmp_path):
    locked = tmp_path / "read-only"
    locked.mkdir()
    locked.chmod(0o500)
    yield locked / "runs"
    locked.chmod(0o700)


def test_premarket_check_failure_survives_read_only_runs(
    tmp_path, monkeypatch, capsys, read_only_runs
):
    # Sandboxed units see TREX_PREMARKET_RUNS read-only; the verdict still decides.
    config = {"source": str(tmp_path), "release_head": "a" * 40}
    monkeypatch.setattr(premarket, "config", lambda: config)

    def refuse(c):
        raise ValueError("release_identity_changed:static_sha256")

    monkeypatch.setattr(premarket, "verify", refuse)
    monkeypatch.setattr(premarket, "RUNS", read_only_runs)
    assert premarket.main(["--check"]) == 1
    out = capsys.readouterr()
    assert "PermissionError" in out.err
    assert "release_identity_changed:static_sha256" in out.err
    assert str(read_only_runs.parent) not in out.out + out.err
    assert not read_only_runs.exists()


def test_premarket_check_pass_writes_nothing(tmp_path, monkeypatch, capsys, read_only_runs):
    config = {"source": str(tmp_path), "release_head": "a" * 40}
    monkeypatch.setattr(premarket, "config", lambda: config)
    monkeypatch.setattr(premarket, "verify", lambda c: tmp_path)
    monkeypatch.setattr(premarket, "RUNS", read_only_runs)
    assert premarket.main(["--check"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_premarket_receipt_modes_still_require_their_report(tmp_path, monkeypatch, read_only_runs):
    # Only the gate tolerates an unwritable report; a run's receipt is its product.
    config = {"source": str(tmp_path), "release_head": "a" * 40}
    monkeypatch.setattr(premarket, "config", lambda: config)

    def refuse(c):
        raise ValueError("release_identity_changed:static_sha256")

    monkeypatch.setattr(premarket, "verify", refuse)
    monkeypatch.setattr(premarket, "RUNS", read_only_runs)
    with pytest.raises(PermissionError):
        premarket.main(["--preview"])


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


def _release_dir(path, manifest):
    """The directory TREX_RELEASE_DIR names: it holds the sealed config.json."""
    path.mkdir(parents=True)
    (path / "config.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return path


def _copy_templates(source):
    (source / "deploy/product").mkdir(parents=True)
    for template in REPO_TEMPLATES.glob("*.in"):
        (source / "deploy/product" / template.name).write_text(template.read_text())


@pytest.fixture
def rendered(release_config, tmp_path):
    _copy_templates(Path(release_config["source"]))
    manifest = release.seal(release_config)
    units = _release_dir(tmp_path / "release", manifest) / "units"
    release.render_units(manifest, units)
    return manifest, units


@pytest.mark.parametrize("state", ["missing", "other_manifest", "not_json"])
def test_render_refuses_release_dir_whose_config_is_not_the_manifest(
    release_config, tmp_path, state
):
    # Rendered units set TREX_RELEASE_DIR to the destination's parent; the gate
    # verifies whatever config.json it finds there, so it must be this manifest.
    _copy_templates(Path(release_config["source"]))
    manifest = release.seal(release_config)
    release_dir = tmp_path / "release"
    if state == "missing":
        release_dir.mkdir()
    elif state == "other_manifest":
        _release_dir(release_dir, {**manifest, "first_session": "2026-10-02"})
    else:
        release_dir.mkdir()
        (release_dir / "config.json").write_text("{")
    with pytest.raises(ValueError, match="release_dir_config_mismatch"):
        release.render_units(manifest, release_dir / "units")
    assert not (release_dir / "units").exists()


def test_render_refuses_relative_destination(release_config, tmp_path, monkeypatch):
    # A relative TREX_RELEASE_DIR would resolve against WorkingDirectory=<source>,
    # not against the directory whose config.json render checked.
    _copy_templates(Path(release_config["source"]))
    manifest = release.seal(release_config)
    _release_dir(tmp_path / "release", manifest)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="release_dir_unsafe_for_unit"):
        release.render_units(manifest, Path("release/units"))
    assert not (tmp_path / "release/units").exists()


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


def test_every_rendered_unit_running_release_code_is_gated(rendered):
    # Full units as well as drop-ins: anything that runs <source> code checks the
    # release identity first, from the release interpreter, against its own dir.
    manifest, units = rendered
    source = manifest["source"]
    gate = f"{source}/.venv/bin/python -m tree_options.trex.premarket --check"
    gated = set()
    for unit in sorted(units.iterdir()):
        pairs = _directives(unit.read_text())
        commands = [v for k, v in pairs if k in release.EXEC_KEYS and v]
        if not any(t.startswith(source + "/") for v in commands for t in v.split()):
            continue
        gated.add(unit.name)
        assert ("Environment", f"TREX_RELEASE_DIR={units.parent}") in pairs, unit.name
        assert ("WorkingDirectory", source) in pairs, unit.name
        assert ("Environment", f"PYTHONPATH={source}/src") in pairs, unit.name
        first = next(v for k, v in pairs if k in release.EXEC_KEYS and v)
        assert first == gate, (unit.name, first)
        for key, value in pairs:
            if key == "Environment" and value.startswith("TREX_PREMARKET_RUNS="):
                assert value == f"TREX_PREMARKET_RUNS={manifest['premarket_runs']}", unit.name
    services = {p.name for p in units.glob("*.service")}
    dropins = {f"{name}-release.conf" for name in RELEASE_DROPINS}
    assert services | dropins == gated
    assert {"trex-evidence-publish.service", "trex-experiments-publish.service"} <= gated


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
    release_dir = _release_dir(tmp_path / "release", manifest)
    with pytest.raises(ValueError, match="release_template_command_unbound"):
        release.render_units(manifest, release_dir / "units")
    # All or nothing: a refused render leaves no partial unit set to install.
    assert not (release_dir / "units").exists()


def test_render_is_pure(rendered, release_config, tmp_path, monkeypatch):
    manifest, _ = rendered

    def forbidden(*args, **kwargs):
        raise AssertionError(f"render ran a process: {args}")

    for name in ("run", "check_output", "check_call", "call", "Popen"):
        monkeypatch.setattr(release.subprocess, name, forbidden)
    units = _release_dir(tmp_path / "again", manifest) / "units"
    release.render_units(manifest, units)
    assert sorted(p.name for p in units.iterdir())
