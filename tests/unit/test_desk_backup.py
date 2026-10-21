"""Phase 0 owner custody: consistent runtime backups and quarantined restores.

Oracles are independent of the implementation: file hashes are recomputed
here with hashlib over the SOURCE bytes, expected file sets are spelled out
literally, and the hook is driven through the real ``DeskRuntime.beat`` over
the shared gateway fake (no broker, no network, tmp dirs only).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.trex_fakes import SupervisedGateway
from tree_options.trex.clock import ET, session_calendar
from tree_options.trex.desk_backup import (
    BACKUP_REQUEST,
    BACKUP_SCHEMA,
    KEEP,
    BackupError,
    RestoreRefused,
    default_backup_root,
    main,
    restore,
    snapshot,
    verify,
)
from tree_options.trex.desk_runtime import DeskPaths, DeskRuntime
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.supervised import SupervisedPaths
from tree_options.trex.supervised_ibkr import SUPERVISED_CLIENT_ID

ACCOUNT = "DU1234567"
EPOCH = "desk83-1a2b3c"
SESSION_DAY = date(2026, 10, 21)  # a Wednesday session
NEXT_SESSION = date(2026, 10, 22)
SATURDAY = date(2026, 10, 24)
TAKEN = datetime(2026, 10, 21, 20, 30, 0, tzinfo=UTC)  # 16:30 ET

#: what every backup must hold (locks, tmp files and requests-to-back-up excluded)
EXPECTED_FILES = {
    "run/book.json",
    "run/events.jsonl",
    "run/monitor.json",
    "run/owner.json",
    "run/profile.json",
    "run/HALT",
    "run/FLATTEN",
    "run/RESOLVE-FLAT-dv1",
    "run/RESOLVE-FLAT-old.applied",
    "run/specs/dv1.json",
    "run/requests/sup-001.claimed.json",
    "run/requests/sup-001.result.json",
    "run/requests/sup-002.json",
    "supervised/HALT",
    "supervised/mandate.json",
    "supervised/mandate.expired-mandate-1.json",
    "supervised/mandate.revoked.json",
    "supervised/permits/permit-a.consumed.json",
    "supervised/permits/permit-b.issued.json",
    "supervised/outbox/sup-001.terminal.json",
    "supervised/journal/sup-001.jsonl",
}
#: authority and consumed-command state: never active after a restore
QUARANTINED = {
    "run/owner.json",
    "run/FLATTEN",
    "run/RESOLVE-FLAT-dv1",
    "run/RESOLVE-FLAT-old.applied",
    "run/requests/sup-001.claimed.json",
    "run/requests/sup-001.result.json",
    "run/requests/sup-002.json",
    "supervised/mandate.json",
    "supervised/mandate.expired-mandate-1.json",
    "supervised/permits/permit-a.consumed.json",
    "supervised/permits/permit-b.issued.json",
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _roots(tmp: Path) -> tuple[Path, SupervisedPaths]:
    """A realistic owner state: run dir + supervised root, plus the noise
    a consistent backup must leave out (locks, tmp files, a request)."""
    run = tmp / "state" / "desk-paper"
    sup = SupervisedPaths(tmp / "state" / "supervised")
    book = {
        "heartbeat": "2026-10-21T16:29:55-04:00",
        "structures": {"dv1": {"status": "open", "filled_qty": 1, "entry_fill": "0.9"}},
    }
    _write(run / "book.json", json.dumps(book, indent=2) + "\n")
    _write(run / "events.jsonl", '{"event":"registered"}\n{"event":"entry_filled"}\n')
    _write(run / "monitor.json", json.dumps({"at": 1.0, "last_error": None}))
    _write(run / "owner.json", json.dumps({"owner_epoch": EPOCH, "client_id": 83}))
    _write(run / "profile.json", json.dumps({"profile_id": "canary-5k"}))
    _write(run / "HALT", "")
    _write(run / "FLATTEN", "")
    _write(run / "RESOLVE-FLAT-dv1", "")
    _write(run / "RESOLVE-FLAT-old.applied", "")
    _write(run / "specs" / "dv1.json", json.dumps({"structure": {"id": "dv1"}}))
    _write(run / "requests" / "sup-001.claimed.json", json.dumps({"effect": "x"}))
    _write(run / "requests" / "sup-001.result.json", json.dumps({"status": "sent"}))
    _write(run / "requests" / "sup-002.json", json.dumps({"effect": "unclaimed"}))
    _write(run / "runtime.lock", "")
    _write(run / ".book.json.123.456.tmp", "half a book")
    _write(run / "monitor.json.tmp", "half")
    _write(run / BACKUP_REQUEST, "")
    _write(sup.root / ".lock", "")
    _write(sup.root / "HALT", json.dumps({"schema": "supervised-effect-halt/1"}))
    _write(sup.mandate(), json.dumps({"owner_epoch": EPOCH, "mandate_id": "mandate-2"}))
    _write(sup.root / "mandate.expired-mandate-1.json", json.dumps({"mandate_id": "mandate-1"}))
    _write(sup.mandate_revoked(), json.dumps({"mandate_id": "mandate-0", "revoked_at": "x"}))
    _write(sup.permit_consumed("permit-a"), json.dumps({"permit_id": "permit-a"}))
    _write(sup.permit("permit-b"), json.dumps({"permit_id": "permit-b"}))
    _write(sup.terminal("sup-001"), json.dumps({"outcome": "acknowledged"}))
    _write(sup.journal("sup-001"), '{"record":"intent"}\n')
    _write(sup.root / "mandate.json.tmp", "half a mandate")
    return run, sup


def _source(run: Path, sup: SupervisedPaths, rel: str) -> Path:
    key, _, tail = rel.partition("/")
    return (run if key == "run" else sup.root) / tail


def _manifest(backup: Path) -> dict[str, Any]:
    return json.loads((backup / "manifest.json").read_text())


def _visible(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if not p.name.startswith("."))


# ------------------------------------------------------------ snapshot


def test_snapshot_copies_both_roots_under_a_hashed_manifest(tmp_path):
    run, sup = _roots(tmp_path)
    dest = tmp_path / "backups"
    backup = snapshot(run, sup, dest, now=TAKEN)
    assert backup.parent == dest
    assert backup.name == f"20261021T203000Z-{EPOCH}"
    doc = _manifest(backup)
    assert doc["schema"] == BACKUP_SCHEMA == "trex-desk-backup/1"
    assert doc["owner_epoch"] == EPOCH, "named from owner.json"
    assert datetime.fromisoformat(doc["taken_at"]) == TAKEN
    assert doc["source_roots"] == {"run": str(run), "supervised": str(sup.root)}
    listed = {f["path"]: f for f in doc["files"]}
    assert set(listed) == EXPECTED_FILES, "locks, tmp files and BACKUP-REQUEST stay out"
    for rel, entry in listed.items():
        raw = _source(run, sup, rel).read_bytes()
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest(), rel
        assert entry["bytes"] == len(raw), rel
        assert (backup / rel).read_bytes() == raw, rel
    assert _visible(dest) == [backup.name], "one complete backup, nothing else"


def test_snapshot_never_leaves_a_partial_backup_visible(tmp_path):
    run, sup = _roots(tmp_path)
    dest = tmp_path / "backups"
    unreadable = run / "specs" / "zz.json"  # copied last within run/specs
    _write(unreadable, "{}")
    unreadable.chmod(0)
    try:
        with pytest.raises(OSError):
            snapshot(run, sup, dest, now=TAKEN)
        assert not dest.exists() or _visible(dest) == [], "tmp-then-rename: no half backup"
    finally:
        unreadable.chmod(0o644)
    backup = snapshot(run, sup, dest, now=TAKEN)
    assert _visible(dest) == [backup.name]
    assert sorted(p.name for p in dest.iterdir()) == [backup.name], "the stale partial is swept"


def test_snapshot_refuses_a_symlink_in_a_live_root(tmp_path):
    run, sup = _roots(tmp_path)
    (run / "specs" / "link.json").symlink_to(run / "book.json")
    dest = tmp_path / "backups"
    with pytest.raises(BackupError) as refused:
        snapshot(run, sup, dest, now=TAKEN)
    assert refused.value.reason == "not_a_regular_file"
    assert not dest.exists() or _visible(dest) == []


@pytest.mark.parametrize("inside", ["run", "supervised"])
def test_snapshot_refuses_a_backup_root_inside_a_live_root(tmp_path, inside):
    run, sup = _roots(tmp_path)
    dest = (run if inside == "run" else sup.root) / "backups"
    with pytest.raises(BackupError) as refused:
        snapshot(run, sup, dest, now=TAKEN)
    assert refused.value.reason == "destination_inside_live_root"
    assert not dest.exists(), "nothing written into the live root"


def test_snapshot_keeps_the_newest_fourteen_and_never_touches_foreign_dirs(tmp_path):
    run, sup = _roots(tmp_path)
    dest = tmp_path / "backups"
    (dest / "operator-notes").mkdir(parents=True)
    made = [
        snapshot(run, sup, dest, now=datetime(2026, 10, 1 + i, 20, 30, tzinfo=UTC)).name
        for i in range(KEEP + 2)
    ]
    assert KEEP == 14
    assert _visible(dest) == sorted([*made[2:], "operator-notes"])


def test_snapshot_in_the_same_second_gets_its_own_directory(tmp_path):
    run, sup = _roots(tmp_path)
    dest = tmp_path / "backups"
    first = snapshot(run, sup, dest, now=TAKEN)
    second = snapshot(run, sup, dest, now=TAKEN)
    assert first != second
    assert verify(first)["taken_at"] == verify(second)["taken_at"]


def test_snapshot_without_an_owner_file_is_named_no_epoch(tmp_path):
    run, sup = _roots(tmp_path)
    (run / "owner.json").unlink()
    backup = snapshot(run, sup, tmp_path / "backups", now=TAKEN)
    assert backup.name == "20261021T203000Z-no-epoch"
    assert _manifest(backup)["owner_epoch"] is None


def test_snapshot_waits_for_then_refuses_a_busy_supervised_lock(tmp_path):
    run, sup = _roots(tmp_path)
    with open(sup.lock(), "a+b") as other:  # a grant/revoke CLI mid-write
        fcntl.flock(other.fileno(), fcntl.LOCK_EX)
        with pytest.raises(BackupError) as refused:
            snapshot(run, sup, tmp_path / "backups", now=TAKEN, lock_wait_s=0.1)
    assert refused.value.reason == "supervised_state_busy"


def test_default_backup_root_is_beside_the_run_dir(tmp_path):
    assert (
        default_backup_root(tmp_path / "trex" / "desk-paper") == tmp_path / "trex" / "desk-backups"
    )


# -------------------------------------------------------------- verify


def test_verify_accepts_an_intact_backup(tmp_path):
    run, sup = _roots(tmp_path)
    backup = snapshot(run, sup, tmp_path / "backups", now=TAKEN)
    assert {f["path"] for f in verify(backup)["files"]} == EXPECTED_FILES


@pytest.mark.parametrize("damage", ["flip_byte", "truncate", "delete", "extra", "traversal"])
def test_verify_refuses_any_damage(tmp_path, damage):
    run, sup = _roots(tmp_path)
    backup = snapshot(run, sup, tmp_path / "backups", now=TAKEN)
    book = backup / "run" / "book.json"
    if damage == "flip_byte":
        raw = bytearray(book.read_bytes())
        raw[0] ^= 0x01
        book.write_bytes(bytes(raw))
    elif damage == "truncate":
        book.write_bytes(book.read_bytes()[:-1])
    elif damage == "delete":
        book.unlink()
    elif damage == "extra":
        _write(backup / "run" / "planted.json", "{}")
    else:
        doc = _manifest(backup)
        doc["files"][0]["path"] = "run/../../escape.json"
        (backup / "manifest.json").write_text(json.dumps(doc))
    with pytest.raises(BackupError):
        verify(backup)


# ------------------------------------------------------------- restore


def test_restore_quarantines_authority_and_restores_the_rest(tmp_path):
    run, sup = _roots(tmp_path)
    backup = snapshot(run, sup, tmp_path / "backups", now=TAKEN)
    scratch = tmp_path / "scratch"
    receipt = restore(backup, scratch, now=TAKEN)
    for rel in EXPECTED_FILES:
        raw = _source(run, sup, rel).read_bytes()
        active, archived = scratch / rel, scratch / "archive" / rel
        if rel in QUARANTINED:
            assert not active.exists(), f"{rel} must never be active authority"
            assert archived.read_bytes() == raw, rel
        else:
            assert active.read_bytes() == raw, rel
            assert not archived.exists(), rel
    assert set(receipt["quarantined"]) == QUARANTINED
    assert set(receipt["restored"]) == EXPECTED_FILES - QUARANTINED
    assert receipt["backup_owner_epoch"] == EPOCH
    on_disk = json.loads((scratch / "RESTORE.json").read_text())
    assert on_disk["quarantined"] == receipt["quarantined"]


def test_restore_refuses_a_damaged_backup_before_writing_anything(tmp_path):
    run, sup = _roots(tmp_path)
    backup = snapshot(run, sup, tmp_path / "backups", now=TAKEN)
    (backup / "supervised" / "mandate.json").write_text("{}")
    scratch = tmp_path / "scratch"
    with pytest.raises(BackupError):
        restore(backup, scratch, now=TAKEN)
    assert not scratch.exists()


def _destinations(tmp_path: Path, run: Path, sup: SupervisedPaths) -> dict[str, Path]:
    elsewhere = tmp_path / "elsewhere-live"
    elsewhere.mkdir()
    (tmp_path / "via-link").symlink_to(run)
    occupied = tmp_path / "occupied"
    _write(occupied / "x", "")
    return {
        "run_dir": run,
        "inside_run_dir": run / "restore",
        "supervised_root": sup.root,
        "inside_supervised": sup.root / "outbox" / "restore",
        "contains_live_roots": tmp_path / "state",
        "inside_backup_root": tmp_path / "backups" / "restore",
        "symlink_into_run_dir": tmp_path / "via-link" / "restore",
        "extra_live_root": elsewhere / "restore",
        "not_empty": occupied,
    }


@pytest.mark.parametrize(
    "where",
    [
        "run_dir",
        "inside_run_dir",
        "supervised_root",
        "inside_supervised",
        "contains_live_roots",
        "inside_backup_root",
        "symlink_into_run_dir",
        "extra_live_root",
        "not_empty",
    ],
)
def test_restore_refuses_any_destination_touching_a_live_root(tmp_path, where):
    run, sup = _roots(tmp_path)
    backup = snapshot(run, sup, tmp_path / "backups", now=TAKEN)
    before = {p: p.read_bytes() for p in sorted(run.rglob("*")) if p.is_file()}
    target = _destinations(tmp_path, run, sup)[where]
    with pytest.raises(RestoreRefused):
        restore(backup, target, live_roots=[tmp_path / "elsewhere-live"], now=TAKEN)
    after = {p: p.read_bytes() for p in sorted(run.rglob("*")) if p.is_file()}
    assert after == before, "a refused restore wrote nothing into the live root"


# ----------------------------------------------------------------- CLI


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def test_cli_snapshot_verify_restore_round_trip(tmp_path, capsys):
    run, sup = _roots(tmp_path)
    dest = tmp_path / "backups"
    rc, out, _ = _cli(
        capsys,
        "snapshot",
        "--run-dir",
        str(run),
        "--supervised-dir",
        str(sup.root),
        "--dest",
        str(dest),
    )
    assert rc == 0
    backup = Path(json.loads(out)["backup"])
    assert backup.parent == dest and (backup / "manifest.json").exists()
    rc, out, _ = _cli(capsys, "verify", str(backup))
    assert (rc, json.loads(out)["files"]) == (0, len(EXPECTED_FILES))
    scratch = tmp_path / "scratch"
    rc, out, _ = _cli(
        capsys,
        "restore",
        str(backup),
        "--into",
        str(scratch),
        "--run-dir",
        str(run),
        "--supervised-dir",
        str(sup.root),
    )
    assert rc == 0
    assert set(json.loads(out)["quarantined"]) == QUARANTINED
    assert (scratch / "archive" / "supervised" / "mandate.json").exists()


def test_cli_snapshot_refuses_while_the_owner_holds_the_run_dir(tmp_path, capsys):
    run, sup = _roots(tmp_path)
    dest = tmp_path / "backups"
    with open(DeskPaths(run).lock(), "a+b") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, _, err = _cli(
            capsys,
            "snapshot",
            "--run-dir",
            str(run),
            "--supervised-dir",
            str(sup.root),
            "--dest",
            str(dest),
        )
    assert rc == 2
    assert "owner_running" in err and BACKUP_REQUEST in err
    assert not dest.exists() or _visible(dest) == []


def test_cli_refuses_a_live_destination_and_a_damaged_backup(tmp_path, capsys):
    run, sup = _roots(tmp_path)
    backup = snapshot(run, sup, tmp_path / "backups", now=TAKEN)
    rc, _, err = _cli(
        capsys,
        "restore",
        str(backup),
        "--into",
        str(run / "restore"),
        "--run-dir",
        str(run),
        "--supervised-dir",
        str(sup.root),
    )
    assert rc == 2 and "refused" in err
    (backup / "run" / "book.json").write_text("{}")
    rc, _, err = _cli(capsys, "verify", str(backup))
    assert rc == 2 and "refused" in err


# ------------------------------------------------- the owner's beat hook


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


class Owner:
    """A runtime over tmp dirs, as the desk process holds it."""

    def __init__(self, tmp: Path, now: datetime, backup_root: Path | None = None) -> None:
        self.gw = SupervisedGateway({}, ACCOUNT)
        self.ib = IbkrTrex(client_id=SUPERVISED_CLIENT_ID)
        self.ib._ib = self.gw
        self.clock = Clock(now)
        self.paths = DeskPaths(tmp / "trex" / "desk-paper")
        self.sup = SupervisedPaths(tmp / "trex" / "supervised")
        self.sup.prepare()
        _write(self.sup.mandate(), json.dumps({"owner_epoch": EPOCH}))
        self.backup_root = backup_root
        self.notified: list[tuple[str, str, str]] = []
        self.rt = self._runtime()
        _write(self.paths.root / "owner.json", json.dumps({"owner_epoch": EPOCH}))

    def _runtime(self) -> DeskRuntime:
        kwargs: dict[str, Any] = {}
        if self.backup_root is not None:
            kwargs["backup_root"] = self.backup_root
        rt = DeskRuntime(self.ib, self.paths, supervised=self.sup, clock=self.clock, **kwargs)
        rt.acquire()
        rt.notify = lambda title, message, priority="default": self.notified.append(
            (title, message, priority)
        )
        return rt

    def restart(self) -> None:
        self.rt.release()
        self.rt = self._runtime()

    def beat(self, at: datetime) -> None:
        self.clock.now = at
        self.rt.beat()

    def backups(self) -> list[str]:
        root = self.backup_root or self.paths.root.parent / "desk-backups"
        return _visible(root) if root.exists() else []

    def events(self, name: str) -> list[dict[str, Any]]:
        if not self.paths.events().exists():
            return []
        rows = [json.loads(line) for line in self.paths.events().read_text().splitlines()]
        return [r for r in rows if r["event"] == name]

    def request(self) -> Path:
        path = self.paths.root / BACKUP_REQUEST
        path.touch()
        return path


@pytest.fixture()
def owner(tmp_path: Path) -> Owner:
    assert session_calendar().is_session(SESSION_DAY)
    assert session_calendar().is_session(NEXT_SESSION)
    assert not session_calendar().is_session(SATURDAY)
    o = Owner(tmp_path, _at(SESSION_DAY, 16, 20))
    yield o
    o.rt.release()


def test_a_backup_request_is_consumed_and_snapshotted_on_the_next_beat(owner):
    request = owner.request()
    owner.beat(_at(SESSION_DAY, 16, 20))  # after the session, before the schedule
    assert not request.exists(), "consumed"
    assert [p for p in owner.paths.root.iterdir() if p.name.startswith(BACKUP_REQUEST)] == []
    (name,) = owner.backups()
    assert name.endswith(f"-{EPOCH}"), "named for the owner epoch in owner.json"
    (taken,) = owner.events("backup_taken")
    assert (taken["trigger"], taken["backup"]) == ("request", name)
    backup = owner.paths.root.parent / "desk-backups" / name
    paths = {f["path"] for f in verify(backup)["files"]}
    assert {"run/book.json", "run/owner.json", "supervised/mandate.json"} <= paths
    owner.beat(_at(SESSION_DAY, 16, 21))
    assert owner.backups() == [name], "one request, one backup"


def test_a_backup_request_waits_out_the_session(owner):
    request = owner.request()
    owner.beat(_at(SESSION_DAY, 10, 0))  # a beat inside 09:30-16:15 never snapshots
    assert request.exists() and owner.backups() == []
    owner.beat(_at(SESSION_DAY, 16, 15))
    assert request.exists() and owner.backups() == [], "16:15 is still the session"
    owner.beat(_at(SESSION_DAY, 16, 16))
    assert not request.exists() and len(owner.backups()) == 1


def test_the_scheduled_backup_runs_once_per_session_day_after_1630(owner):
    owner.beat(_at(SESSION_DAY, 16, 29))
    assert owner.backups() == []
    owner.beat(_at(SESSION_DAY, 16, 30))
    (first,) = owner.backups()
    assert [e["trigger"] for e in owner.events("backup_taken")] == ["scheduled"]
    owner.beat(_at(SESSION_DAY, 16, 45))
    owner.beat(_at(SESSION_DAY, 23, 59))
    assert owner.backups() == [first]
    owner.restart()  # a restarted owner finds today's backup on disk
    owner.beat(_at(SESSION_DAY, 18, 0))
    assert owner.backups() == [first]
    owner.beat(_at(NEXT_SESSION, 9, 0))  # before the open: not yet
    owner.beat(_at(NEXT_SESSION, 16, 31))
    assert len(owner.backups()) == 2
    owner.beat(_at(SATURDAY, 17, 0))  # no session, no scheduled backup
    assert len(owner.backups()) == 2
    owner.request()
    owner.beat(_at(SATURDAY, 17, 1))  # a request is honoured any day
    assert len(owner.backups()) == 3


def test_a_failed_backup_never_raises_into_the_owner_loop(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the backup root should be")
    owner = Owner(tmp_path, _at(SESSION_DAY, 16, 31), backup_root=blocked)
    try:
        request = owner.request()
        owner.beat(_at(SESSION_DAY, 16, 31))  # must not raise
        assert not request.exists(), "the request is consumed even when the backup fails"
        book = json.loads(owner.paths.book().read_text())
        assert datetime.fromisoformat(book["heartbeat"]) == _at(SESSION_DAY, 16, 31)
        (failed,) = owner.events("backup_failed")
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", failed["error"]), "the error class only"
        assert str(blocked) not in json.dumps(failed), "never the message"
        assert any("backup_failed" in n[0] for n in owner.notified), "the operator hears it"
        owner.beat(_at(SESSION_DAY, 16, 40))
        assert len(owner.events("backup_failed")) == 1, "one scheduled attempt a day"
        assert owner.events("backup_taken") == []
    finally:
        owner.rt.release()


def test_the_backup_root_is_configurable(tmp_path):
    elsewhere = tmp_path / "custody"
    owner = Owner(tmp_path, _at(SESSION_DAY, 16, 31), backup_root=elsewhere)
    try:
        owner.beat(_at(SESSION_DAY, 16, 31))
        assert len(_visible(elsewhere)) == 1
        assert not (owner.paths.root.parent / "desk-backups").exists()
    finally:
        owner.rt.release()


def test_the_beat_hook_backup_is_consistent_with_the_heartbeat_it_wrote(owner):
    owner.beat(_at(SESSION_DAY, 16, 30))
    (name,) = owner.backups()
    backup = owner.paths.root.parent / "desk-backups" / name
    copied = json.loads((backup / "run" / "book.json").read_text())
    live = json.loads(owner.paths.book().read_text())
    assert copied == live, "taken under the lock after this beat's heartbeat"
    assert os.path.getsize(backup / "manifest.json") > 0
