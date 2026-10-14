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
