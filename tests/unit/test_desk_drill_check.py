"""drill-check: the Stage-B runbook's gates, evaluated against fixture
state dirs (fresh / stale / absent), with every external surface faked.

Oracles are the runbook's own thresholds (heartbeat < 30 s, OI >= 100,
spread <= 10% of mid, > 30 min from the Sunday cold restart) and the
production writers (grant_mandate, the desk's file shapes) — never
drill-check internals. The skip-vs-fail rule is asserted on exit codes:
a SKIPPED gate never fails the run, a measured miss always does.
"""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta
from decimal import Decimal

from tree_options.trex import drill_check
from tree_options.trex.clock import ET
from tree_options.trex.desk_cli import _cli
from tree_options.trex.desk_runtime import DeskPaths
from tree_options.trex.supervised import SupervisedPaths, grant_mandate

#: Thu 2026-10-01 10:00 ET: a session day, inside RTH, 3d 2h before the
#: Sunday 12:00 ET cold restart
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=ET)
SATURDAY = datetime(2026, 10, 3, 10, 0, tzinfo=ET)
SUNDAY_NEAR_RESTART = datetime(2026, 10, 4, 11, 45, tzinfo=ET)
EPOCH = "desk83-test01"
OLD_EPOCH = "desk83-yesterday0"
PID = 111111
DIGEST = "a" * 64

PROFILE = {
    "profile_id": "paper-canary",
    "revision": 1,
    "intended_capital": "10000",
    "risk_style": "steady",
    "goals": ["protect-capital"],
    "allowed_strategy_versions": ["operational-canary/1"],
}

#: hand-computed grant budget: base 1 + one under-using window (94.0 left
#: vs 51.9 planned) = 2 orders, below the cap of 3
EXPECTED_MAX_ORDERS = 2


#: the desk's own structure spec, as the runtime writes it (LegStructure
#: JSON): a 0.74 cap debit vertical, so an open package with no fill on
#: record risks 0.74 x 100 = $74.
DESK_SPEC = {
    "id": "drill-a",
    "underlying": "SPY",
    "kind": "debit_vertical",
    "entry_date": "2026-09-30",
    "exit_deadline": "2026-10-09",
    "quantity": 1,
    "limit": "0.74",
    "legs": [
        {"right": "P", "action": "BUY", "strike": "740.0", "expiry": "2026-10-16"},
        {"right": "P", "action": "SELL", "strike": "735.0", "expiry": "2026-10-16"},
    ],
    "exits": {"touch": True, "breach": False},
}


def _write_spec(specs, doc):
    specs.mkdir(parents=True, exist_ok=True)
    (specs / f"{doc['id']}.json").write_text(json.dumps(doc))


def _green_world(tmp_path, now=NOW):
    """The runbook's all-green state on disk: fresh epoch, beating book,
    clean monitor, settled gateway, quota snapshot, profile. The legacy
    plans dir is present and EMPTY (the desk is the account's only book,
    which is the state G2's account segment must report as such) and the
    desk book's open structure has its spec, so the account reads."""
    root = tmp_path / "trex"
    desk = root / "desk-paper"
    desk.mkdir(parents=True, exist_ok=True)
    (root / "plans").mkdir(exist_ok=True)
    _write_spec(desk / "specs", DESK_SPEC)
    (desk / "owner.json").write_text(
        json.dumps(
            {
                "owner_epoch": EPOCH,
                "client_id": 83,
                "pid": PID,
                "started_at": (now - timedelta(hours=2)).isoformat(),
            }
        )
    )
    (desk / "book.json").write_text(
        json.dumps(
            {
                "heartbeat": (now - timedelta(seconds=3)).isoformat(),
                "structures": {
                    "drill-a": {"status": "open", "filled_qty": 1, "exit_filled_qty": 0}
                },
            }
        )
    )
    (desk / "monitor.json").write_text(
        json.dumps(
            {
                "at": (now - timedelta(seconds=4)).timestamp(),
                "connected": True,
                "tick_failures": 0,
                "last_tick_ok_at": (now - timedelta(seconds=4)).timestamp(),
            }
        )
    )
    (desk / "profile.json").write_text(json.dumps(PROFILE))
    (desk / "quota-windows.json").write_text(
        json.dumps(
            {
                "schema": "desk-quota-windows/1",
                "windows": [
                    {
                        "name": "zai",
                        "actual_left_pct": "94.0",
                        "planned_left_pct": "51.9",
                        "resets_at": "15:29",
                        "dry": False,
                    }
                ],
            }
        )
    )
    (root / "gateway.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "detail": "server_version=176",
                "since": (now - timedelta(hours=1)).timestamp(),
                "checked_at": (now - timedelta(seconds=10)).timestamp(),
            }
        )
    )
    return root, desk


_REAL_DEPS = drill_check.Deps  # bound before any test monkeypatches the name


def _deps(now=NOW, unit="active", pid_alive=True, probe=None):
    def unit_state(name):
        assert isinstance(name, str) and name
        return unit

    return _REAL_DEPS(
        unit_state=unit_state,
        pid_alive=lambda pid: pid == PID and pid_alive,
        clock=lambda: now,
        probe=probe if probe is not None else _probe_must_not_run,
    )


def _probe_must_not_run(pair):
    raise AssertionError(f"probe must not run here ({pair})")


def _grant(
    sup_dir, *, epoch=EPOCH, account="DUT143714", ttl=12 * 3600, now=NOW, granted_hours_ago=0.0
):
    return grant_mandate(
        SupervisedPaths(sup_dir),
        now=now - timedelta(hours=granted_hours_ago),
        account_id=account,
        owner_epoch=epoch,
        strategy_version="operational-canary/1",
        profile_digest=DIGEST,
        max_orders=2,
        ttl_seconds=ttl,
        granted_by="operator-terminal",
    )


def _run(root, *, deps, pair=None, sup_dir=None, rail=None):
    lines: list[str] = []
    rc = drill_check.run_drill_check(
        DeskPaths(root / "desk-paper"),
        SupervisedPaths(sup_dir or root / "supervised"),
        deps=deps,
        pair=pair,
        gateway_state=root / "gateway.json",
        exit_watch_state=root / "exit_watch.json",
        max_account_open_loss=rail,
        plans_root=root / "plans",
        out=lines.append,
    )
    return rc, "\n".join(lines)


def _rewrite(path, **changes):
    doc = json.loads(path.read_text())
    doc.update(changes)
    path.write_text(json.dumps(doc))


# ----------------------------------------------------------------- G2


def _tree_bytes(root):
    """Every file under ``root``, by relative path: the desk run dir now has
    a specs/ subdir, so a top-level read is not the whole state any more."""
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def test_all_green_is_drill_go(tmp_path):
    root, desk = _green_world(tmp_path)
    _grant(root / "supervised")
    before = _tree_bytes(desk)

    rc, text = _run(root, deps=_deps())

    assert rc == 0
    assert "G2 desk: GO (unit trex-desk.service active" in text
    assert f"epoch {EPOCH} (started" in text and "fresh" in text
    assert f"pid {PID} alive" in text and "heartbeat 3s" in text
    assert "monitor 4s old" in text and "exit_watch ok (desk book guarded)" in text
    assert "G3 gateway: GO (gateway ok, settled" in text
    assert "cold-restart in 3d 2h" in text
    assert f"G4 mandate: GO (granted for epoch {EPOCH}" in text
    assert "G5 quotes: SKIPPED (no candidate strikes given" in text
    assert "G6 rehearsal: MANUAL" in text
    assert "DRILL: GO (G5 skipped)" in text
    # read-only: the run dir is byte-identical and no watchdog state appeared
    assert _tree_bytes(desk) == before
    assert not (root / "exit_watch.json").exists()


def test_stale_heartbeat_fails_g2_only(tmp_path):
    root, desk = _green_world(tmp_path)
    _grant(root / "supervised")
    _rewrite(desk / "book.json", heartbeat=(NOW - timedelta(seconds=60)).isoformat())

    rc, text = _run(root, deps=_deps())

    assert rc == 1
    assert "G2 desk: NO-GO" in text and "heartbeat stale 1m" in text
    assert "G4 mandate: GO" in text  # one red gate fails the drill, not the rest
    assert "DRILL: NO-GO (G2 failed)" in text


def test_absent_monitor_json_fails_g2(tmp_path):
    root, desk = _green_world(tmp_path)
    _grant(root / "supervised")
    (desk / "monitor.json").unlink()

    rc, text = _run(root, deps=_deps())

    assert rc == 1 and "monitor.json absent/unreadable" in text


def test_disconnected_or_failing_monitor_fails_g2(tmp_path):
    root, desk = _green_world(tmp_path / "a")
    _grant(root / "supervised")
    _rewrite(desk / "monitor.json", connected=False, tick_failures=0)

    rc, text = _run(root, deps=_deps())
    assert rc == 1 and "monitor.json not connected" in text

    root, desk = _green_world(tmp_path / "b")
    _grant(root / "supervised")
    _rewrite(desk / "monitor.json", connected=True, tick_failures=2)

    rc, text = _run(root, deps=_deps())
    assert rc == 1 and "monitor.json tick_failures=2" in text


def test_inactive_unit_or_dead_pid_fails_g2(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")

    rc, text = _run(root, deps=_deps(unit="failed"))
    assert rc == 1 and "unit trex-desk.service failed" in text

    rc, text = _run(root, deps=_deps(pid_alive=False))
    assert rc == 1 and f"pid {PID} is not running" in text


def test_epoch_that_predates_the_nightly_logoff_is_stale(tmp_path):
    root, desk = _green_world(tmp_path)
    _grant(root / "supervised")
    _rewrite(desk / "owner.json", started_at=(NOW - timedelta(days=3)).isoformat())

    rc, text = _run(root, deps=_deps())

    assert rc == 1
    assert "stale" in text and "before the nightly gateway logoff" in text


def test_missing_owner_json_fails_g2_and_g4(tmp_path):
    root, desk = _green_world(tmp_path)
    (desk / "owner.json").unlink()

    rc, text = _run(root, deps=_deps())

    assert rc == 1
    assert "owner.json has no owner_epoch" in text
    assert "G4 mandate: NO-GO (no owner epoch" in text
    assert "--owner-epoch <epoch from owner.json>" in text  # never a guess


def test_exit_watch_bad_verdict_fails_g2(tmp_path):
    root, desk = _green_world(tmp_path)
    _grant(root / "supervised")
    # a book that stopped beating: exit_watch must call it monitor_down
    _rewrite(desk / "book.json", heartbeat=(NOW - timedelta(minutes=10)).isoformat())

    rc, text = _run(root, deps=_deps())

    assert rc == 1
    assert "exit_watch monitor_down" in text


# ------------------------------------------------------------- G2 account truth

#: the LIVE account (verified 2026-09-29, kept as the fixture's oracle):
#: two NVDA put spreads in a legacy book, 0.21 x 5 x 100 = 105 and
#: 1.24 x 3 x 100 = 372, so $477, with time stops 2026-10-09 / 2026-11-06.
LEGACY_PLAN = """
id = "putspread-20260922"
account_mode = "paper"
total_debit_cap = 1840.00
entry_window_start = "09:45"
entry_window_end = "12:00"

[[structures]]
id = "nvda-oct"
underlying = "NVDA"
entry_date = 2026-09-22
expiry = 2026-10-16
long_strike = 185.0
short_strike = 150.0
quantity = 5
limit_cap = 0.50
exit_deadline = 2026-10-09

[[structures]]
id = "qqq-nov"
underlying = "QQQ"
entry_date = 2026-09-22
expiry = 2026-11-20
long_strike = 600.0
short_strike = 475.0
quantity = 4
limit_cap = 2.40
exit_deadline = 2026-11-06

[[structures]]
id = "nvda-nov"
underlying = "NVDA"
entry_date = 2026-09-22
expiry = 2026-11-20
long_strike = 185.0
short_strike = 150.0
quantity = 3
limit_cap = 2.10
exit_deadline = 2026-11-06
"""

OPEN_LEGACY_BOOK = {
    "heartbeat": (NOW - timedelta(seconds=3)).isoformat(),
    "structures": {
        "nvda-oct": {"status": "open", "entry_fill": "0.21", "filled_qty": 5, "exit_filled_qty": 0},
        "qqq-nov": {"status": "closed", "filled_qty": 0, "exit_filled_qty": 0},
        "nvda-nov": {"status": "open", "entry_fill": "1.24", "filled_qty": 3, "exit_filled_qty": 0},
    },
}

#: the G2 account segment exactly as it will read on the live desk
LIVE_ACCOUNT_SEGMENT = (
    "account: 4 legs / 2 structures - $477.00 max loss outside the desk book "
    "(legacy:putspread-20260922/nvda-oct @2026-10-09, nvda-nov @2026-11-06); "
    "desk book: no exposure"
)


def _legacy_world(tmp_path, *, now=NOW, flat_desk=True, book=None):
    """The green world PLUS the live legacy book on the same account
    (``putspread-20260922``, monitored: a fresh heartbeat and clean tick
    health, so exit_watch guards it too). ``flat_desk`` is the LIVE desk
    shape — an empty desk book and no specs, exactly as on disk today —
    which is the state that used to print a bare "no exposure"."""
    root, desk = _green_world(tmp_path, now=now)
    if flat_desk:
        shutil.rmtree(desk / "specs")
        _rewrite(desk / "book.json", structures={})
    (root / "plans" / "2026-09-22.toml").write_text(LEGACY_PLAN)
    legacy = root / "putspread-20260922"
    legacy.mkdir()
    (legacy / "book.json").write_text(json.dumps(OPEN_LEGACY_BOOK if book is None else book))
    (legacy / "monitor.json").write_text(
        json.dumps(
            {"at": (now - timedelta(seconds=4)).timestamp(), "connected": True, "tick_failures": 0}
        )
    )
    return root, desk


def test_g2_names_the_legacy_book_the_legs_the_deadlines_and_the_total(tmp_path):
    root, _desk = _legacy_world(tmp_path)
    _grant(root / "supervised")

    rc, text = _run(root, deps=_deps())

    assert rc == 0
    assert LIVE_ACCOUNT_SEGMENT in text
    # the exit_watch note must not hide the other books' rows either
    assert (
        "exit_watch ok (desk book: no exposure; other book(s) under the "
        "scan root with exposure: putspread-20260922)"
    ) in text
    assert "exit_watch ok (no exposure)" not in text
    # the final summary carries the account total, rail or no rail
    assert (
        "DRILL: GO (G5 skipped) [" in text
        and "account open loss $477.00 account-wide (2 structures outside "
        "the desk book, earliest exit 2026-10-09; desk book: no "
        "exposure) - rail UNSET"
        in text
    )


def test_an_empty_desk_book_never_reads_as_a_flat_account(tmp_path):
    """The exact misleading case, pinned: the desk book has no row in
    exit_watch and no positions, while another service's book holds $477."""
    root, desk = _legacy_world(tmp_path)
    _grant(root / "supervised")

    rc, text = _run(root, deps=_deps())

    assert json.loads((desk / "book.json").read_text())["structures"] == {}
    assert rc == 0  # unset rail: today's behavior, the drill is not blocked
    assert "account: 4 legs / 2 structures" in text
    assert "desk book: no exposure" in text
    assert "(no exposure)" not in text.split("DRILL:")[-1]


def test_both_books_are_reported_and_never_merged_into_one_number(tmp_path):
    root, _desk = _legacy_world(tmp_path, flat_desk=False)
    _grant(root / "supervised")

    rc, text = _run(root, deps=_deps())

    # the desk row: drill-a is open with no fill on record, so it sits at
    # its cap, 0.74 x 100 x 1 = $74. Both halves are named, neither stands
    # in for the other, and the account total is the sum only where asked.
    assert rc == 0
    assert (
        "account: 4 legs / 2 structures - $477.00 max loss outside the "
        "desk book (legacy:putspread-20260922/nvda-oct @2026-10-09, "
        "nvda-nov @2026-11-06); desk book: 2 legs / 1 structure - "
        "$74.00 max loss"
    ) in text
    assert (
        "exit_watch ok (desk book guarded; other book(s) under the scan "
        "root with exposure: putspread-20260922)"
    ) in text
    assert "account open loss $551.00 account-wide" in text


def test_an_unreadable_book_says_so_and_never_claims_no_exposure(tmp_path):
    root, _desk = _legacy_world(tmp_path, book="not json")

    _grant(root / "supervised")
    rc, text = _run(root, deps=_deps())

    # exit_watch already fails a book it cannot read (its own rule, since
    # before this lane); the account segment must not read that as flat
    assert rc == 1
    assert "exit_watch monitor_down" in text
    assert "putspread-20260922/book.json unreadable" in text
    assert "account: exposure NOT COUNTABLE" in text
    assert "putspread-20260922/book.json unreadable (ValueError)" in text
    assert "no positions outside the desk book" not in text
    assert "account open loss NOT COUNTABLE (" in text.split("DRILL:")[-1]


def test_the_flag_is_off_unset_and_only_bites_when_the_total_exceeds_it(tmp_path):
    root, _desk = _legacy_world(tmp_path)
    _grant(root / "supervised")

    rc, text = _run(root, deps=_deps())
    assert rc == 0 and "rail UNSET (informational, no exposure gate)" in text

    rc, text = _run(root, deps=_deps(), rail=Decimal("476.99"))
    assert rc == 1
    assert "G2 desk: NO-GO" in text
    assert "account open loss $477.00 exceeds the --max-account-open-loss rail $476.99" in text
    assert "DRILL: NO-GO (G2 failed)" in text
    assert "rail $476.99 EXCEEDED" in text

    rc, text = _run(root, deps=_deps(), rail=Decimal("477.00"))
    assert rc == 0  # exactly at the rail is not over it
    assert "within the $477.00 rail" in text

    rc, text = _run(root, deps=_deps(), rail=Decimal("0"))
    assert rc == 1 and "exceeds the --max-account-open-loss rail $0.00" in text


def test_a_rail_against_an_uncountable_account_fails_closed(tmp_path):
    root, _desk = _legacy_world(tmp_path, book="not json")
    _grant(root / "supervised")

    rc, text = _run(root, deps=_deps(), rail=Decimal("10000"))

    assert rc == 1
    assert (
        "account open loss NOT COUNTABLE against the "
        "--max-account-open-loss rail $10000.00 (fail closed)"
    ) in text
    assert (
        "account open loss NOT COUNTABLE (0 structures read, 1 book "
        "problem)" in text.split("DRILL:")[-1]
    )


def test_the_account_rail_counts_the_desk_book_too(tmp_path):
    root, _desk = _legacy_world(tmp_path, flat_desk=False)
    _grant(root / "supervised")

    # 477 legacy + 74 desk = 551: the legacy book alone passes a 500 rail,
    # and the desk's own working entry is what turns it red
    rc, _ = _run(root, deps=_deps(), rail=Decimal("500"))
    assert rc == 1
    rc, text = _run(root, deps=_deps(), rail=Decimal("551"))
    assert rc == 0 and "within the $551.00 rail" in text
    rc, text = _run(root, deps=_deps(), rail=Decimal("550.99"))
    assert rc == 1 and "account open loss $551.00 exceeds" in text


def test_account_truth_never_writes_a_byte(tmp_path):
    root, _desk = _legacy_world(tmp_path)
    _grant(root / "supervised")
    before = {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }

    rc, _ = _run(root, deps=_deps(), rail=Decimal("1000"))

    assert rc == 0
    after = {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }
    assert after == before  # no exit_watch.json, no lock, no temp file


def test_the_cli_carries_the_flag_and_refuses_a_negative_one(tmp_path, monkeypatch, capsys):
    root, desk = _legacy_world(tmp_path)
    _grant(root / "supervised")
    monkeypatch.setenv("TREX_SUPERVISED_DIR", str(root / "supervised"))
    monkeypatch.setattr(drill_check, "Deps", lambda: _deps())
    argv = [
        "--dir",
        str(desk),
        "drill-check",
        "--plans-dir",
        str(root / "plans"),
        "--gateway-state",
        str(root / "gateway.json"),
        "--exit-watch-state",
        str(root / "exit_watch.json"),
    ]

    assert _cli([*argv, "--max-account-open-loss", "1000"]) == 0
    out = capsys.readouterr().out
    assert LIVE_ACCOUNT_SEGMENT in out
    assert "within the $1000.00 rail" in out

    assert _cli([*argv, "--max-account-open-loss", "10"]) == 1
    assert "exceeds the --max-account-open-loss rail $10" in capsys.readouterr().out

    assert _cli([*argv, "--max-account-open-loss", "-1"]) == 2
    assert "usage error: --max-account-open-loss must be >= 0" in capsys.readouterr().err
    assert not (root / "exit_watch.json").exists()  # still read-only


# ----------------------------------------------------------------- G3


def test_gateway_needs_2fa_fails_g3(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")
    _rewrite(
        root / "gateway.json", status="needs_2fa", detail="waiting for 2FA approval in IBKR Mobile"
    )

    rc, text = _run(root, deps=_deps())

    assert rc == 1 and "gateway needs_2fa" in text


def test_silent_gateway_watch_fails_g3(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")
    _rewrite(root / "gateway.json", checked_at=(NOW - timedelta(minutes=10)).timestamp())

    rc, text = _run(root, deps=_deps())

    assert rc == 1 and "gateway watch silent or stale" in text


def test_gateway_ok_but_not_settled_fails_g3(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")
    _rewrite(root / "gateway.json", since=(NOW - timedelta(seconds=60)).timestamp())

    rc, text = _run(root, deps=_deps())

    assert rc == 1 and "gateway ok but not settled (recovered 1m ago" in text


def test_sunday_cold_restart_window_fails_g3(tmp_path):
    root, _desk = _green_world(tmp_path, now=SUNDAY_NEAR_RESTART)
    _grant(root / "supervised", now=SUNDAY_NEAR_RESTART)

    rc, text = _run(root, deps=_deps(now=SUNDAY_NEAR_RESTART))

    assert rc == 1
    assert "Sunday cold-restart window in 15m (< 30 min)" in text


def test_next_sunday_noon_wraps_the_week():
    monday = datetime(2026, 10, 5, 9, 0, tzinfo=ET)  # Mon after the drill week
    assert drill_check.next_sunday_noon_et(monday) == datetime(2026, 10, 11, 12, tzinfo=ET)
    after_noon_sunday = datetime(2026, 10, 4, 12, 1, tzinfo=ET)
    assert drill_check.next_sunday_noon_et(after_noon_sunday) == datetime(
        2026, 10, 11, 12, tzinfo=ET
    )


def test_last_nightly_logoff_uses_denver_1745():
    before_logoff = datetime(2026, 10, 1, 19, 44, tzinfo=ET)  # 17:44 Denver
    assert drill_check.last_nightly_logoff(before_logoff) == datetime(
        2026, 9, 30, 19, 45, tzinfo=ET
    )  # Wednesday's logoff
    after_logoff = datetime(2026, 10, 1, 19, 46, tzinfo=ET)
    assert drill_check.last_nightly_logoff(after_logoff) == datetime(2026, 10, 1, 19, 45, tzinfo=ET)


# ----------------------------------------------------------------- G4


def test_absent_mandate_prints_the_exact_grant_command_with_live_epoch(tmp_path):
    root, _desk = _green_world(tmp_path)

    rc, text = _run(root, deps=_deps())

    assert rc == 1
    assert f"G4 mandate: NO-GO (mandate absent (epoch {EPOCH}))" in text
    assert (
        "    grant: python -m tree_options.trex.supervised grant "
        "--account DUT143714 "
        f"--owner-epoch {EPOCH} "
        "--strategy operational-canary/1 "
        "--profile-digest " in text
    )
    # hand-computed budget: base 1 + one under-using window, and a 12 h TTL
    assert f"--max-orders {EXPECTED_MAX_ORDERS} --ttl-seconds 43200" in text
    assert text.count("grant: ") == 1


def test_mandate_for_an_old_epoch_is_no_go_with_the_fresh_command(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised", epoch=OLD_EPOCH)

    rc, text = _run(root, deps=_deps())

    assert rc == 1
    assert f"mandate binds OLD epoch {OLD_EPOCH}; current epoch {EPOCH}" in text
    # the printed command binds the CURRENT epoch, never the stale one
    assert f"--owner-epoch {EPOCH}" in text
    assert f"--owner-epoch {OLD_EPOCH}" not in text


def test_exhausted_entry_mandate_is_no_go_without_stopping_protective_exits(tmp_path):
    root, _desk = _green_world(tmp_path)
    paths = SupervisedPaths(root / "supervised")
    mandate = _grant(paths.root)
    exhausted = mandate.model_copy(update={"orders_used": mandate.max_orders})
    paths.mandate().write_text(exhausted.model_dump_json(by_alias=True))

    rc, text = _run(root, deps=_deps())

    assert rc == 1
    assert "G4 mandate: NO-GO (entry budget exhausted (2/2)" in text
    assert "protective exits remain owned by the desk runtime" in text
    assert not (root / "desk-paper" / "HALT").exists()


def test_one_remaining_entry_permit_is_go(tmp_path):
    root, _desk = _green_world(tmp_path)
    paths = SupervisedPaths(root / "supervised")
    mandate = _grant(paths.root)
    paths.mandate().write_text(
        mandate.model_copy(update={"orders_used": 1}).model_dump_json(by_alias=True)
    )

    rc, text = _run(root, deps=_deps())

    assert rc == 0
    assert "G4 mandate: GO" in text
    assert "1 order(s) left" in text


def test_expired_or_wrong_account_mandate_is_no_go(tmp_path):
    root, _desk = _green_world(tmp_path / "a")
    _grant(root / "supervised", ttl=3600, granted_hours_ago=2.0)

    rc, text = _run(root, deps=_deps())
    assert rc == 1 and "expired for epoch" in text

    root, _desk = _green_world(tmp_path / "b")
    _grant(root / "supervised", account="DU9999999")

    rc, text = _run(root, deps=_deps())
    assert rc == 1 and "binds account DU9999999" in text


# ----------------------------------------------------------------- G5


PAIR = drill_check.StrikePair(
    underlying="SPY",
    buy_strike=Decimal("744"),
    sell_strike=Decimal("742"),
    expiry=date(2026, 11, 20),
)


def _probe(legs):
    return lambda _pair: drill_check.ProbeOutcome(available=True, legs=legs)


def test_g5_skips_without_strikes_and_does_not_fail_the_exit_code(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")

    rc, text = _run(root, deps=_deps())

    assert rc == 0 and "G5 quotes: SKIPPED (no candidate strikes given" in text


def test_g5_skips_outside_rth_without_probing(tmp_path):
    root, _desk = _green_world(tmp_path, now=SATURDAY)
    _grant(root / "supervised", now=SATURDAY)

    rc, text = _run(root, deps=_deps(now=SATURDAY), pair=PAIR)

    assert rc == 0
    assert "G5 quotes: SKIPPED (no live quotes outside RTH" in text


def test_g5_skips_when_the_probe_surface_is_unavailable(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")
    probe = lambda _pair: drill_check.ProbeOutcome(  # noqa: E731
        available=False, reason="ConnectionRefusedError"
    )

    rc, text = _run(root, deps=_deps(probe=probe), pair=PAIR)

    assert rc == 0
    assert "G5 quotes: SKIPPED (no live quotes: probe unavailable (ConnectionRefusedError))" in text


def test_g5_go_on_a_liquid_two_sided_rail(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")
    legs = (
        drill_check.LegQuote(Decimal("744"), Decimal("7.89"), Decimal("7.93"), 500),
        drill_check.LegQuote(Decimal("742"), Decimal("7.53"), Decimal("7.56"), 400),
    )

    rc, text = _run(root, deps=_deps(probe=_probe(legs)), pair=PAIR)

    assert rc == 0
    # hand-computed oracle: (7.93-7.89)/7.91 = 0.5% of mid
    assert (
        "G5 quotes: GO (744 7.89/7.93 spread 0.5% of mid, OI 500; "
        "742 7.53/7.56 spread 0.4% of mid, OI 400)"
    ) in text
    assert "DRILL: GO [" in text  # nothing skipped now


def test_g5_fails_on_thin_oi(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")
    legs = (
        drill_check.LegQuote(Decimal("744"), Decimal("7.89"), Decimal("7.93"), 500),
        drill_check.LegQuote(Decimal("742"), Decimal("7.53"), Decimal("7.56"), 50),
    )

    rc, text = _run(root, deps=_deps(probe=_probe(legs)), pair=PAIR)

    assert rc == 1 and "742 OI 50 < 100" in text


def test_g5_fails_on_a_wide_spread(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")
    # (8.00-7.20)/7.60 = 10.5% of mid > 10%
    legs = (
        drill_check.LegQuote(Decimal("744"), Decimal("7.20"), Decimal("8.00"), 500),
        drill_check.LegQuote(Decimal("742"), Decimal("7.53"), Decimal("7.56"), 400),
    )

    rc, text = _run(root, deps=_deps(probe=_probe(legs)), pair=PAIR)

    assert rc == 1 and "spread 10.5% of mid > 10%" in text


def test_g5_fails_when_a_leg_is_one_sided(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")
    one_sided = (
        drill_check.LegQuote(Decimal("744"), None, Decimal("7.93"), 500),
        drill_check.LegQuote(Decimal("742"), Decimal("7.53"), Decimal("7.56"), 400),
    )

    rc, text = _run(root, deps=_deps(probe=_probe(one_sided)), pair=PAIR)

    assert rc == 1 and "744: not two-sided" in text


def test_g5_skips_rather_than_fakes_when_the_api_reports_no_oi(tmp_path):
    """The paper API does not report option open interest (verified
    2026-09-29: no OI tick, history and regulatory snapshots refused). The
    measured spread still shows, the missing datum is named, and the gate
    SKIPS (operator verifies OI in TWS) instead of failing or faking."""
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")
    no_oi = (
        drill_check.LegQuote(Decimal("744"), Decimal("7.89"), Decimal("7.93"), None),
        drill_check.LegQuote(Decimal("742"), Decimal("7.53"), Decimal("7.56"), None),
    )

    rc, text = _run(root, deps=_deps(probe=_probe(no_oi)), pair=PAIR)

    assert rc == 0
    assert "G5 quotes: SKIPPED (" in text
    assert "spread 0.5% of mid" in text and "OI not reported" in text
    assert "verify OI >= 100 in TWS" in text


def test_g5_missing_oi_does_not_mask_a_measured_miss(tmp_path):
    root, _desk = _green_world(tmp_path)
    _grant(root / "supervised")
    # a genuinely bad rail beside a missing OI: the measured miss decides
    legs = (
        drill_check.LegQuote(Decimal("744"), Decimal("7.20"), Decimal("8.00"), None),
        drill_check.LegQuote(Decimal("742"), Decimal("7.53"), Decimal("7.56"), None),
    )

    rc, text = _run(root, deps=_deps(probe=_probe(legs)), pair=PAIR)

    assert rc == 1 and "spread 10.5% of mid > 10%" in text


# ------------------------------------------------------------------ CLI


def test_cli_drill_check_end_to_end(tmp_path, monkeypatch, capsys):
    root, desk = _green_world(tmp_path)
    _grant(root / "supervised")
    monkeypatch.setenv("TREX_SUPERVISED_DIR", str(root / "supervised"))
    monkeypatch.setattr(drill_check, "Deps", lambda: _deps())

    rc = _cli(
        [
            "--dir",
            str(desk),
            "drill-check",
            "--plans-dir",
            str(root / "plans"),
            "--gateway-state",
            str(root / "gateway.json"),
            "--exit-watch-state",
            str(root / "exit_watch.json"),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("G2 desk: ")
    assert (
        "account: no positions outside the desk book; desk book: 2 legs / "
        "1 structure - $74.00 max loss"
    ) in out
    assert "DRILL: GO (G5 skipped)" in out
    assert not (root / "exit_watch.json").exists()  # the dry run wrote nothing


def test_cli_drill_check_exits_1_when_a_gate_fails(tmp_path, monkeypatch, capsys):
    root, desk = _green_world(tmp_path)  # no mandate granted
    monkeypatch.setenv("TREX_SUPERVISED_DIR", str(root / "supervised"))
    monkeypatch.setattr(drill_check, "Deps", lambda: _deps())

    rc = _cli(
        [
            "--dir",
            str(desk),
            "drill-check",
            "--plans-dir",
            str(root / "plans"),
            "--gateway-state",
            str(root / "gateway.json"),
            "--exit-watch-state",
            str(root / "exit_watch.json"),
        ]
    )

    assert rc == 1
    out = capsys.readouterr().out
    assert "G4 mandate: NO-GO" in out and "grant: python -m" in out


def test_cli_drill_check_partial_strike_args_are_a_usage_error(tmp_path, capsys):
    rc = _cli(["--dir", str(tmp_path), "drill-check", "--buy-strike", "744"])
    assert rc == 2
    assert "usage error" in capsys.readouterr().err
