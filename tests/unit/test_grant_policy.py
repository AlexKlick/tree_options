"""The daily grant policy: base + quota-spare extra, fail-closed on unknowns."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tree_options.trex.grant_policy import (
    DEFAULT_BASE,
    WINDOWS_SCHEMA,
    QuotaWindow,
    daily_grant,
    grant_command,
    load_windows,
    windows_path,
)


def _window(name: str, actual: str, planned: str | None, **kw) -> QuotaWindow:
    return QuotaWindow(
        name=name,
        actual_left_pct=Decimal(actual),
        planned_left_pct=None if planned is None else Decimal(planned),
        **kw,
    )


def test_base_only_when_no_windows_or_none_spare():
    assert daily_grant(()).total_orders == DEFAULT_BASE
    plan = daily_grant((_window("zai", "40.0", "50.0"),))  # over-using: no extra
    assert (plan.base_orders, plan.extra_orders, plan.total_orders) == (1, 0, 1)
    assert any("on plan or over-using" in r for r in plan.reasons)


def test_each_under_using_window_adds_one_extra_capped():
    windows = (
        _window("minimax", "58.0", "20.5", resets_at="14:00"),
        _window("zai", "94.0", "51.9", resets_at="15:29"),
    )
    plan = daily_grant(windows)
    assert (plan.base_orders, plan.extra_orders, plan.total_orders) == (1, 2, 3)
    assert not plan.capped
    assert any("minimax under-using" in r and "14:00" in r for r in plan.reasons)
    crowded = (*windows, _window("zencat", "100.0", "99.4"))
    capped = daily_grant(crowded)
    assert (capped.extra_orders, capped.total_orders) == (3, 3)
    assert capped.capped, "the rails' cap binds, not the quota"


def test_unknown_plan_and_dry_windows_earn_no_extra():
    # the dry window IS under-using by numbers: only the dry guard holds it back
    plan = daily_grant((_window("x", "90.0", None), _window("y", "80.0", "50.0", dry=True)))
    assert plan.extra_orders == 0
    assert any("plan unknown" in r for r in plan.reasons)
    assert any("dry" in r for r in plan.reasons)


def test_cap_below_base_refuses():
    with pytest.raises(ValueError, match="cap cannot be below base"):
        daily_grant((), base=3, cap=2)


def test_snapshot_round_trip(tmp_path: Path):
    path = tmp_path / "quota-windows.json"
    path.write_text(
        json.dumps(
            {
                "schema": WINDOWS_SCHEMA,
                "windows": [
                    {
                        "name": "zai",
                        "actual_left_pct": "94.0",
                        "planned_left_pct": "51.9",
                        "resets_at": "15:29",
                        "dry": False,
                    },
                    {"name": "minimax", "actual_left_pct": "58.0", "planned_left_pct": "20.5"},
                ],
            }
        )
    )
    windows = load_windows(path)
    assert daily_grant(windows).total_orders == 3
    assert windows_path().name == "quota-windows.json"


def test_a_bad_snapshot_refuses_never_defaults(tmp_path: Path):
    path = tmp_path / "quota-windows.json"
    path.write_text(json.dumps({"schema": "wrong/1", "windows": []}))
    with pytest.raises(ValueError, match="schema"):
        load_windows(path)


def test_the_emitted_command_is_runnable_verbatim():
    plan = daily_grant((_window("zai", "94.0", "51.9"),))
    command = grant_command(
        plan,
        account="DUT143714",
        owner_epoch="desk83-x",
        strategy="operational-canary/1",
        profile_digest="a" * 64,
        ttl_seconds=43200,
        granted_by="alex",
    )
    assert "--max-orders 2" in command
    assert "--ttl-seconds 43200" in command
    assert "--owner-epoch desk83-x" in command


def test_apply_grants_the_computed_budget(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TREX_SUPERVISED_DIR", str(tmp_path / "supervised"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "desk").mkdir()
    snapshot = tmp_path / "desk" / "quota-windows.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema": WINDOWS_SCHEMA,
                "windows": [{"name": "zai", "actual_left_pct": "94.0", "planned_left_pct": "51.9"}],
            }
        )
    )
    from tree_options.trex.grant_policy import _cli

    rc = _cli(
        [
            "--windows",
            str(snapshot),
            "--apply",
            "--owner-epoch",
            "desk83-x",
            "--profile-digest",
            "a" * 64,
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out.splitlines()[-1])["max_orders"] == 2
    # the mandate file exists with the computed budget
    doc = json.loads((tmp_path / "supervised" / "mandate.json").read_text())
    assert doc["max_orders"] == 2


def test_apply_without_epoch_or_digest_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TREX_SUPERVISED_DIR", str(tmp_path / "supervised"))
    from tree_options.trex.grant_policy import _cli

    rc = _cli(["--apply"])
    assert rc == 2
    assert "--apply needs" in capsys.readouterr().err
