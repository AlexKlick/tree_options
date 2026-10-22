"""Wave 3 lane C: ``GET /api/today`` (``trex-today/1``), the Today read model.

Oracles are never this lane's code: the frozen contract models
(``TodayView``, ``AcceptanceSummary.of``) and fixtures, the repo calendar
and the helpers that already own each rule (``canary.equity_entry_window``,
``equity_exits.forced_exit_at``, ``trex.clock.calendar_sessions_left``,
``exit_watch.HEARTBEAT_STALE_S``), the Wave 2 writers (``CommandStore``,
``trex.pause``) and the Wave 2 readers whose detail pages Today summarizes
(``/api/proposals``, ``/api/portfolio``). Every source failure must read as
``unavailable`` with a reason, never as a zero, an empty list or "clear".
"""

from __future__ import annotations

import ast
import fcntl
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.trex_fakes import (
    approve_command,
    contract_fixture,
    entry_proposal,
    equity_effect,
)
from tree_options.action_graph.canary import equity_entry_window, equity_entry_window_blockers
from tree_options.time.calendar import CalendarIntegrityError
from tree_options.time.sessions import shift_instant
from tree_options.trex import pause
from tree_options.trex.clock import ET, calendar_sessions_left, session_calendar
from tree_options.trex.contracts import acceptance as acc
from tree_options.trex.contracts import wave3 as w
from tree_options.trex.contracts.canonical import model_bytes, model_document
from tree_options.trex.contracts.commands import CommandResult, parse_command
from tree_options.trex.contracts.effects import SupervisedEffect
from tree_options.trex.contracts.layout import Wave2RunDir
from tree_options.trex.contracts.proposals import proposal_bytes, proposal_sha256
from tree_options.trex.desk_backup import SCHEDULED_AT
from tree_options.trex.desk_runtime import DEFAULT_ENTRY_WINDOW
from tree_options.trex.engine import EngineConfig
from tree_options.trex.equity_exits import deadline_session, forced_exit_at
from tree_options.trex.exit_watch import HEARTBEAT_STALE_S
from tree_options.trex_web import today_view as tv
from tree_options.trex_web.command_store import CommandStore
from tree_options.trex_web.commands_view import proposals_payload
from tree_options.trex_web.gateway_view import WATCH_STALE_AFTER_S
from tree_options.trex_web.portfolio_view import portfolio_payload

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "src/tree_options/trex_web/today_view.py"
FIXTURES = REPO / "tests/contracts/fixtures/wave3"

#: Thursday 2026-10-22, 10:02 ET: a regular session is open
NOW = datetime(2026, 10, 22, 14, 2, tzinfo=UTC)
CAL = session_calendar()
#: the owner epoch the Wave 2 proposal fixtures were composed under
EPOCH = contract_fixture("proposal_equity.json")["review"]["owner_epoch"]
OWNER = "owner@example.com"
ACCOUNT = "DU0000001"
OPTION_INTENT = "opt-qqq-001"
EQUITY_INTENT = "eq-spy-001"
NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")


def iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def ago(instant: datetime, seconds: int) -> datetime:
    return shift_instant(instant, -seconds)


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def fixture_keys(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return loaded


def lot(intent: str | None, quantity: int, program: str = "prog-x") -> dict[str, Any]:
    ours = intent is not None
    return {
        "attribution": {
            "intent_id": intent,
            "program_id": program if ours else None,
            "strategy_version": "v/1" if ours else None,
            "sleeve_ref": "unassigned",
        },
        "quantity": quantity,
    }


#: one desk option intent held as two legs, one desk equity intent, and an
#: outside stock lot (no intent): Today counts DISTINCT ATTRIBUTED intents
HOLDING_ROWS: list[dict[str, Any]] = [
    contract_fixture("holding.json"),
    {
        "account_id": ACCOUNT,
        "con_id": 1001,
        "sec_type": "OPT",
        "symbol": "QQQ",
        "quantity": 1,
        "cost_basis_micros": 150_000_000,
        "lots": [lot(OPTION_INTENT, 1)],
    },
    {
        "account_id": ACCOUNT,
        "con_id": 1002,
        "sec_type": "OPT",
        "symbol": "QQQ",
        "quantity": -1,
        "cost_basis_micros": -90_000_000,
        "lots": [lot(OPTION_INTENT, -1)],
    },
    {
        "account_id": ACCOUNT,
        "con_id": 2002,
        "sec_type": "STK",
        "symbol": "AAPL",
        "quantity": 5,
        "cost_basis_micros": 1_000_000_000,
        "lots": [lot(None, 5)],
    },
]


def intents_by_kind(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Hand count of the rows above, by security type."""
    seen: dict[str, set[str]] = {"OPT": set(), "STK": set()}
    for row in rows:
        for one in row["lots"]:
            if one["attribution"]["intent_id"] is not None:
                seen[row["sec_type"]].add(one["attribution"]["intent_id"])
    return {kind: len(ids) for kind, ids in seen.items()}


class World:
    """Every Today source in a temp tree; the clock and probes are injected."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.run_dir = root / "desk-paper"
        self.run_dir.mkdir(parents=True)
        self.supervised = root / "supervised"
        self.gateway_path = root / "trex" / "gateway.json"
        self.acceptance_dir = root / "acceptance"
        self.layout = Wave2RunDir(self.run_dir)
        self.now = NOW
        self.lock: bool | Exception = True
        self.probed: list[Path] = []
        self.calendar: Callable[[], Any] = session_calendar
        self.timers: Callable[[], Sequence[w.ScheduledEvent]] | None = None

    # -- the route -------------------------------------------------------------

    def _probe(self, path: Path) -> bool:
        self.probed.append(path)
        if isinstance(self.lock, Exception):
            raise self.lock
        return self.lock

    def client(self) -> TestClient:
        app = FastAPI()
        tv.attach(
            app,
            run_dir=lambda: self.run_dir,
            supervised_dir=lambda: self.supervised,
            clock=lambda: self.now,
            gateway_state=lambda: self.gateway_path,
            acceptance_dir=lambda: self.acceptance_dir,
            calendar=lambda: self.calendar(),
            lock_probe=self._probe,
            timers=self.timers,
        )
        return TestClient(app)

    def today(self) -> dict[str, Any]:
        response = self.client().get("/api/today")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body: dict[str, Any] = response.json()
        w.TodayView.model_validate(body)  # the frozen contract accepts the page
        return body

    # -- writers (each as its producer writes it) ------------------------------

    def beat(self, age_s: int = 10, structures: dict[str, Any] | None = None) -> None:
        write_json(
            self.run_dir / "book.json",
            {"heartbeat": ago(self.now, age_s).isoformat(), "structures": structures or {}},
        )

    def watch(self, status: str = "ok", age_s: int = 30, detail: str = "API answering") -> None:
        checked = self.now.timestamp() - age_s
        write_json(
            self.gateway_path,
            {"status": status, "since": checked - 600, "detail": detail, "checked_at": checked},
        )

    def owner(self, epoch: str = EPOCH) -> None:
        write_json(
            self.run_dir / "owner.json",
            {"owner_epoch": epoch, "client_id": 7, "pid": 4242, "started_at": iso(NOW)},
        )

    def propose(self, proposal: Any) -> Any:
        path = self.layout.proposal(proposal.proposal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(proposal_bytes(proposal))
        return proposal

    def holdings(self, rows: Sequence[dict[str, Any]] = HOLDING_ROWS, age_s: int = 60) -> None:
        write_json(
            self.layout.holdings(),
            {
                "schema": "trex-holdings/1",
                "account_id": ACCOUNT,
                "as_of": iso(ago(self.now, age_s)),
                "cash_micros": 25_000_000_000,
                "holdings": list(rows),
            },
        )

    def risk(self, age_s: int = 30, **changes: Any) -> dict[str, Any]:
        document = contract_fixture("risk_snapshot.json")
        document["as_of"] = iso(ago(self.now, age_s))
        document.update(changes)
        write_json(self.layout.risk_snapshot(), document)
        return document

    def reconciliation(self, age_s: int = 30, **changes: Any) -> None:
        document = {
            "schema": "trex-reconciliation/1",
            "account_id": ACCOUNT,
            "status": "clean",
            "as_of": iso(ago(self.now, age_s)),
            "mismatches": [],
            "max_age_s": 120,
        }
        document.update(changes)
        write_json(self.layout.reconciliation(), document)

    def accept(self, document: acc.AcceptanceDocument) -> None:
        path = w.AcceptanceDir(self.acceptance_dir).document()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(model_bytes(document))

    def healthy(self) -> World:
        """Every source present and good: the page has no blocker at all."""
        self.beat()
        self.watch()
        self.owner()
        self.propose(entry_proposal())
        self.holdings()
        self.risk()
        self.reconciliation()
        self.accept(acceptance_document())
        return self


def session_record(day: str) -> acc.SessionRecord:
    return acc.SessionRecord.model_validate(
        {
            "session": day,
            "heartbeat_minutes": 390,
            "reconciliation_open": "clean",
            "reconciliation_close": "clean",
            "reconciliation_episodes": [],
            "unowned_orders": 0,
            "max_exit_unguarded_s": 0,
            "unavailable": {},
            "evidence": [],
        }
    )


def acceptance_document() -> acc.AcceptanceDocument:
    """Three qualifying sessions before NOW, judged against the repo calendar."""
    return acc.compute_acceptance(
        sessions=[session_record(d) for d in ("2026-10-19", "2026-10-20", "2026-10-21")],
        lifecycles=[],
        drills=[],
        owners=[OWNER],
        calendar=CAL,
        computed_at=datetime(2026, 10, 21, 21, 0, tzinfo=UTC),
    )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def codes(body: dict[str, Any]) -> list[str]:
    return [b["code"] for b in body["blockers"]]


def blocker(body: dict[str, Any], code: str) -> dict[str, Any]:
    [found] = [b for b in body["blockers"] if b["code"] == code]
    return found


# =============================================================================
# the document
# =============================================================================


def test_today_is_a_trex_today_document_with_every_key(world: World) -> None:
    body = world.healthy().today()
    fixture = fixture_keys("today.json")
    assert body["schema"] == "trex-today/1" == w.TODAY_SCHEMA
    assert body["now"] == iso(NOW)
    assert body["execution_enabled"] is False
    assert body["acceptance"]["live_authorized"] is False
    assert set(body) == set(fixture)
    for block in ("session", "desk_owner", "gateway", "approvals", "acceptance", "next_events"):
        assert set(body[block]) == set(fixture[block]), block
    for block in ("pause_entries", "halt", "flatten", "queued"):
        assert set(body["controls"][block]) == set(fixture["controls"][block]), block
    for block in ("holdings", "risk"):
        assert set(body["positions"][block]) == set(fixture["positions"][block]), block
    [item] = body["approvals"]["items"]
    assert set(item) == set(fixture["approvals"]["items"][0])


def test_a_healthy_desk_shows_no_blocker(world: World) -> None:
    body = world.healthy().today()
    assert body["blockers"] == []
    for block in ("session", "desk_owner", "gateway", "approvals", "acceptance", "next_events"):
        assert body[block]["unavailable_reason"] is None, block
    assert body["desk_owner"]["status"] == "healthy"
    assert body["positions"]["risk"]["status"] == "fresh"


def test_the_page_never_writes(world: World) -> None:
    world.healthy()
    pause.pause_entries(world.run_dir, actor="loopback:operator", reason="lunch", now=ago(NOW, 60))

    def tree() -> dict[str, tuple[int, int, int]]:
        return {
            str(p.relative_to(world.root)): (
                p.lstat().st_mode,
                p.lstat().st_size,
                p.lstat().st_mtime_ns,
            )
            for p in sorted(world.root.rglob("*"))
        }

    before = tree()
    world.today()
    assert tree() == before


def test_a_missing_run_dir_is_never_created(world: World) -> None:
    world.run_dir.rmdir()
    world.today()
    assert not world.run_dir.exists()


def test_every_source_failing_still_answers_with_reasons(world: World) -> None:
    def broken() -> Any:
        raise CalendarIntegrityError("calendar checksum mismatch")

    world.calendar = broken
    world.lock = tv.LockStateUnknown("no /proc/locks")
    world.run_dir.rmdir()
    world.run_dir.write_text("not a directory")  # every run dir read fails
    body = world.today()
    assert body["session"]["status"] == "unavailable"
    assert body["session"]["unavailable_reason"] == "calendar_unreadable"
    assert body["desk_owner"]["unavailable_reason"] == "heartbeat_absent"
    assert body["gateway"]["unavailable_reason"] == "gateway_watch_not_reporting"
    for name in ("pause_entries", "halt", "flatten"):
        control = body["controls"][name]
        assert (control["status"], control["unavailable_reason"]) == (
            "unavailable",
            "kill_file_unreadable",
        )
    assert body["controls"]["queued"]["unavailable_reason"] == "command_store_unreadable"
    assert body["approvals"]["unavailable_reason"] == "proposals_dir_unreadable"
    assert body["positions"]["holdings"]["status"] == "unavailable"
    assert body["positions"]["risk"]["status"] == "unavailable"
    assert body["acceptance"]["unavailable_reason"] == "acceptance_missing"
    assert body["next_events"]["unavailable_reason"] == "calendar_unreadable"
    # the null rule: no unknown figure is rendered as a zero or an empty list
    for block in (body["approvals"], body["positions"]["holdings"], body["positions"]["risk"]):
        figures = {k: v for k, v in block.items() if k not in ("status", "unavailable_reason")}
        assert set(figures.values()) == {None}
    assert {"exit_owner_not_ready", "kill_file_unreadable", "calendar_unreadable"} <= set(
        codes(body)
    )


def test_serving_today_never_loads_a_broker_library(tmp_path: Path) -> None:
    script = f"""
import json, sys
from datetime import UTC, datetime
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tree_options.trex_web.today_view import attach
root = Path({str(tmp_path)!r})
app = FastAPI()
attach(app, run_dir=lambda: root / "run", supervised_dir=lambda: root / "sup",
       clock=lambda: datetime.now(UTC), gateway_state=lambda: root / "gateway.json",
       acceptance_dir=lambda: root / "acc")
code = TestClient(app).get("/api/today").status_code
loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("ib_async", "ib_insync", "eventkit"))
print(json.dumps({{"code": code, "loaded": loaded}}))
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(REPO / "src"),
    }
    done = subprocess.run(
        [sys.executable, "-B", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report == {"code": 200, "loaded": []}


# =============================================================================
# session (the repo NYSE calendar)
# =============================================================================


@pytest.mark.parametrize(
    ("now", "status", "session"),
    [
        (datetime(2026, 10, 22, 12, 0, tzinfo=UTC), "pre_open", date(2026, 10, 22)),
        (datetime(2026, 10, 22, 13, 30, tzinfo=UTC), "open", date(2026, 10, 22)),
        (NOW, "open", date(2026, 10, 22)),
        (datetime(2026, 10, 22, 20, 0, tzinfo=UTC), "after_close", date(2026, 10, 23)),
        (datetime(2026, 10, 24, 15, 0, tzinfo=UTC), "no_session", date(2026, 10, 26)),
        (datetime(2026, 11, 26, 15, 0, tzinfo=UTC), "no_session", date(2026, 11, 27)),
        (datetime(2026, 11, 27, 17, 0, tzinfo=UTC), "open", date(2026, 11, 27)),
        (datetime(2026, 11, 27, 18, 0, tzinfo=UTC), "after_close", date(2026, 11, 30)),
    ],
    ids=["pre-open", "at-open", "open", "at-close", "weekend", "holiday", "early", "early-closed"],
)
def test_session_from_the_repo_calendar(
    world: World, now: datetime, status: str, session: date
) -> None:
    world.now = now
    got = world.today()["session"]
    et_date = now.astimezone(ET).date()
    assert got == {
        "status": status,
        "unavailable_reason": None,
        "et_date": et_date.isoformat(),
        "session": session.isoformat(),
        "opens_at": iso(CAL.session_open(session)),
        "closes_at": iso(CAL.session_close(session)),
        "early_close": session in CAL.early_close_sessions(),
        "calendar_sessions_left": calendar_sessions_left(et_date),
    }


def test_the_last_calendar_session_warns_and_then_the_calendar_is_exhausted(world: World) -> None:
    last = CAL.sessions()[-1]
    world.now = shift_instant(CAL.session_open(last), 3600)
    body = world.today()
    assert body["session"]["calendar_sessions_left"] == 0
    assert blocker(body, "calendar_horizon_near")["severity"] == "warning"
    horizon = [e for e in body["next_events"]["items"] if e["kind"] == "calendar_horizon"]
    assert [e["at"] for e in horizon] == [iso(CAL.session_close(last))]

    world.now = shift_instant(CAL.session_close(last), 60)
    body = world.today()
    assert body["session"]["status"] == "unavailable"
    assert body["session"]["unavailable_reason"] == "calendar_exhausted"
    assert "calendar_exhausted" in codes(body)


def test_far_from_the_horizon_there_is_no_calendar_warning(world: World) -> None:
    body = world.today()
    assert calendar_sessions_left(NOW.date()) >= tv.CALENDAR_HORIZON_WARN_SESSIONS
    assert "calendar_horizon_near" not in codes(body)


# =============================================================================
# desk owner (book.json heartbeat + runtime.lock)
# =============================================================================


@pytest.mark.parametrize(
    ("age_s", "lock", "status"),
    [
        (10, True, "healthy"),
        (HEARTBEAT_STALE_S, True, "healthy"),
        (HEARTBEAT_STALE_S + 1, True, "stale"),
        (10, False, "stopped"),
        (HEARTBEAT_STALE_S + 1, False, "stopped"),
    ],
)
def test_desk_owner_status_by_heartbeat_age_and_lock(
    world: World, age_s: int, lock: bool, status: str
) -> None:
    world.beat(age_s)
    world.lock = lock
    got = world.today()["desk_owner"]
    assert got == {
        "status": status,
        "unavailable_reason": None,
        "heartbeat_at": iso(ago(NOW, age_s)),
        "heartbeat_age_s": age_s,
        "lock_held": lock,
        "exit_owner_ready": status == "healthy",
        "stale_after_s": HEARTBEAT_STALE_S,
    }
    assert world.probed == [world.run_dir / "runtime.lock"]


@pytest.mark.parametrize(
    "book",
    [None, "{not json", {"structures": {}}, {"heartbeat": None}, {"heartbeat": "2026-10-22T14:00"}],
    ids=["missing", "corrupt", "no-heartbeat", "null", "naive"],
)
def test_no_readable_heartbeat_is_unavailable_never_healthy(world: World, book: Any) -> None:
    path = world.run_dir / "book.json"
    if isinstance(book, str):
        path.write_text(book)
    elif book is not None:
        write_json(path, book)
    body = world.today()
    owner = body["desk_owner"]
    assert (owner["status"], owner["unavailable_reason"]) == ("unavailable", "heartbeat_absent")
    assert owner["exit_owner_ready"] is None and owner["lock_held"] is None
    found = blocker(body, "exit_owner_not_ready")
    assert (found["scope"], found["severity"]) == ("exits", "critical")


def test_a_heartbeat_from_the_future_is_not_a_heartbeat(world: World) -> None:
    world.beat(-30)
    assert world.today()["desk_owner"]["unavailable_reason"] == "heartbeat_absent"


def test_an_unknown_lock_state_is_unavailable(world: World) -> None:
    world.beat()
    world.lock = tv.LockStateUnknown("no lock table")
    owner = world.today()["desk_owner"]
    assert (owner["status"], owner["unavailable_reason"]) == ("unavailable", "runtime_lock_unknown")


@pytest.mark.parametrize(
    ("age_s", "lock", "code"), [(200, True, "desk_owner_stale"), (10, False, "desk_owner_stopped")]
)
def test_an_unhealthy_desk_owner_is_a_critical_exit_blocker(
    world: World, age_s: int, lock: bool, code: str
) -> None:
    world.beat(age_s)
    world.lock = lock
    found = blocker(world.today(), code)
    assert (found["scope"], found["severity"]) == ("exits", "critical")


def test_the_proc_locks_probe_sees_a_held_flock(tmp_path: Path) -> None:
    lock = tmp_path / "runtime.lock"
    other = tmp_path / "other.lock"
    lock.touch()
    other.touch()
    assert tv.proc_locks_probe(lock) is False
    with open(other, "a+b") as held_elsewhere:
        fcntl.flock(held_elsewhere.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert tv.proc_locks_probe(lock) is False  # another file's lock is not ours
    with open(lock, "a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert tv.proc_locks_probe(lock) is True
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        assert tv.proc_locks_probe(lock) is False
    assert tv.proc_locks_probe(tmp_path / "never-created.lock") is False


def test_the_proc_locks_probe_fails_closed(tmp_path: Path) -> None:
    lock = tmp_path / "runtime.lock"
    lock.touch()
    with pytest.raises(tv.LockStateUnknown):
        tv.proc_locks_probe(lock, table=tmp_path / "no-such-lock-table")


def test_the_proc_locks_probe_ignores_waiters_and_shared_locks(tmp_path: Path) -> None:
    lock = tmp_path / "runtime.lock"
    lock.touch()
    st = lock.stat()
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    table = tmp_path / "locks"
    table.write_text(
        f"1: FLOCK  ADVISORY  READ  41 {key} 0 EOF\n"
        f"2: -> FLOCK  ADVISORY  WRITE 42 {key} 0 EOF\n"
        f"3: POSIX  ADVISORY  WRITE 43 {key} 0 EOF\n"
    )
    assert tv.proc_locks_probe(lock, table=table) is False
    table.write_text(f"9: FLOCK  ADVISORY  WRITE 44 {key} 0 EOF\n")
    assert tv.proc_locks_probe(lock, table=table) is True


# =============================================================================
# gateway (gateway.json, the watch's own verdict)
# =============================================================================


@pytest.mark.parametrize(
    "status", ["ok", "starting", "checking", "needs_login", "needs_2fa", "api_down", "down"]
)
def test_gateway_watch_status_passes_through(world: World, status: str) -> None:
    world.watch(status, age_s=30, detail="as the watch said")
    assert world.today()["gateway"] == {
        "status": status,
        "unavailable_reason": None,
        "detail": "as the watch said",
        "checked_at": iso(ago(NOW, 30)),
        "age_s": 30,
    }


def test_a_watch_exactly_at_its_stale_bound_still_counts(world: World) -> None:
    world.watch(age_s=WATCH_STALE_AFTER_S)
    assert world.today()["gateway"]["status"] == "ok"


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (None, "gateway_watch_not_reporting"),
        ("{oops", "gateway_watch_not_reporting"),
        ([1, 2], "gateway_watch_not_reporting"),
        ({"status": "fine", "detail": "x", "checked_at": 0}, "gateway_watch_not_reporting"),
        ({"status": "ok", "checked_at": "yesterday", "detail": "x"}, "gateway_watch_not_reporting"),
        ({"status": "ok", "checked_at": True, "detail": "x"}, "gateway_watch_not_reporting"),
        ({"status": "ok", "detail": None}, "gateway_watch_not_reporting"),
        ("stale", "gateway_watch_stale"),
        ("future", "gateway_watch_stale"),
    ],
    ids=[
        "missing",
        "corrupt",
        "list",
        "bad-status",
        "bad-time",
        "bool-time",
        "no-detail",
        "stale",
        "future",
    ],
)
def test_a_missing_or_stale_watch_is_unavailable_never_healthy(
    world: World, document: Any, reason: str
) -> None:
    if document == "stale":
        world.watch(age_s=WATCH_STALE_AFTER_S + 1)
    elif document == "future":
        world.watch(age_s=-120)
    elif isinstance(document, str):
        world.gateway_path.parent.mkdir(parents=True, exist_ok=True)
        world.gateway_path.write_text(document)
    elif document is not None:
        if isinstance(document, dict) and "checked_at" not in document:
            document = {**document, "checked_at": NOW.timestamp()}
        write_json(world.gateway_path, document)
    body = world.today()
    gateway = body["gateway"]
    assert (gateway["status"], gateway["unavailable_reason"]) == ("unavailable", reason)
    assert (gateway["detail"], gateway["checked_at"], gateway["age_s"]) == (None, None, None)
    assert blocker(body, reason)["scope"] == "display"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("needs_login", "gateway_needs_login"),
        ("needs_2fa", "gateway_needs_2fa"),
        ("api_down", "gateway_api_down"),
        ("down", "gateway_down"),
        ("starting", "gateway_starting"),
    ],
)
def test_an_unhealthy_gateway_is_a_blocker(world: World, status: str, code: str) -> None:
    world.watch(status)
    assert code in codes(world.today())


# =============================================================================
# controls: kill files and queued kill-switch commands
# =============================================================================


def test_no_kill_file_reads_clear(world: World) -> None:
    controls = world.today()["controls"]
    for name, file_name in (
        ("pause_entries", "PAUSE_ENTRIES"),
        ("halt", "HALT"),
        ("flatten", "FLATTEN"),
    ):
        assert controls[name] == {
            "name": file_name,
            "status": "clear",
            "unavailable_reason": None,
            "since": None,
            "reason": None,
        }
    assert controls["queued"] == {"status": "ok", "unavailable_reason": None, "items": []}


def test_standing_kill_files_say_since_when_and_why(world: World) -> None:
    placed = ago(NOW, 600)
    pause.pause_entries(world.run_dir, actor="family:owner@example.com", reason="lunch", now=placed)
    # the desk owner's HALT record (trex.desk_commands)
    halt_at = ago(NOW, 300)
    write_json(
        world.run_dir / "HALT",
        {
            "placed_by": "family:owner@example.com",
            "reason": "news",
            "command": "0a9b8c7d-6e5f-4a3b-8c1d-0e9f8a7b6c5d",
            "at": iso(halt_at),
            "issued_at": iso(halt_at),
        },
    )
    # a terminal's bare touch, and a dangling symlink: both stand (lexists)
    flatten = world.run_dir / "FLATTEN"
    flatten.symlink_to(world.run_dir / "nowhere")
    body = world.today()
    controls = body["controls"]
    assert (controls["pause_entries"]["status"], controls["pause_entries"]["since"]) == (
        "standing",
        iso(placed),
    )
    assert controls["pause_entries"]["reason"] == "lunch"
    assert controls["halt"] == {
        "name": "HALT",
        "status": "standing",
        "unavailable_reason": None,
        "since": iso(halt_at),
        "reason": "news",
    }
    assert controls["flatten"]["status"] == "standing"
    assert controls["flatten"]["reason"] == ""
    assert controls["flatten"]["since"] == iso(
        datetime.fromtimestamp(flatten.lstat().st_mtime, UTC)
    )
    halt = blocker(body, "halt")
    assert (halt["scope"], halt["severity"]) == ("exits", "critical")
    assert "Emergency stop (also stops exits)" in halt["message"]
    paused = blocker(body, "entries_paused")
    assert paused["scope"] == "entries"
    assert "flatten" in codes(body)


def test_a_bare_touched_kill_file_stands_with_its_file_time(world: World) -> None:
    path = world.run_dir / "HALT"
    path.touch()
    stamp = ago(NOW, 90).timestamp()
    os.utime(path, (stamp, stamp))
    halt = world.today()["controls"]["halt"]
    assert (halt["status"], halt["since"], halt["reason"]) == (
        "standing",
        iso(datetime.fromtimestamp(stamp, UTC)),
        "",
    )


@NOT_ROOT
def test_an_unreadable_run_dir_makes_kill_files_unavailable_not_clear(world: World) -> None:
    world.run_dir.chmod(0)
    try:
        body = world.today()
    finally:
        world.run_dir.chmod(0o755)
    for name in ("pause_entries", "halt", "flatten"):
        assert body["controls"][name]["unavailable_reason"] == "kill_file_unreadable"
    assert body["approvals"]["unavailable_reason"] == "proposals_dir_unreadable"


def command_doc(kind: str, **changes: Any) -> dict[str, Any]:
    document = contract_fixture("command_pause_entries.json")
    document.update(
        kind=kind,
        issued_at=iso(ago(NOW, 60)),
        expires_at=iso(shift_instant(NOW, 240)),
        **changes,
    )
    return document


def test_queued_lists_accepted_kill_switch_commands_not_yet_applied(world: World) -> None:
    store = CommandStore(world.run_dir)
    queued = parse_command(command_doc("pause_entries"))
    claimed = parse_command(
        command_doc("halt", idempotency_key="11111111-2222-4333-8444-555555555555")
    )
    answered = parse_command(
        command_doc("flatten", idempotency_key="66666666-7777-4888-8999-aaaaaaaaaaaa")
    )
    approve = approve_command(
        entry_proposal(),
        idempotency_key="bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
        issued_at=iso(ago(NOW, 30)),
        expires_at=iso(shift_instant(NOW, 200)),
    )
    for command in (queued, claimed, answered, approve):
        assert store.submit(command, now=NOW).result.status == "accepted"
    layout = world.layout
    # the owner claims by renaming; an answered claim carries the owner's result
    for command in (claimed, answered):
        key = command.idempotency_key
        layout.command(key).rename(layout.commands() / f"{key}.claimed.json")
    applied = CommandResult.model_validate(
        {
            **model_document(store.read_result(answered.idempotency_key)),
            "status": "applied",
            "at": iso(NOW),
        }
    )
    layout.command_result(answered.idempotency_key).write_bytes(model_bytes(applied))
    items = world.today()["controls"]["queued"]["items"]
    expected = [
        {
            "idempotency_key": c.idempotency_key,
            "kind": c.kind,
            "issued_at": iso(c.issued_at),
            "expires_at": iso(c.expires_at),
            "issued_by_sub": c.issued_by.sub,
        }
        for c in sorted((queued, claimed), key=lambda c: (c.issued_at, c.idempotency_key))
    ]
    assert items == expected


def test_an_unreadable_queued_command_makes_the_queue_unknown(world: World) -> None:
    folder = world.layout.commands()
    folder.mkdir(parents=True)
    (folder / "0a9b8c7d-6e5f-4a3b-8c1d-0e9f8a7b6c5d.json").write_text("{garbage")
    queued = world.today()["controls"]["queued"]
    assert (queued["status"], queued["items"]) == ("unavailable", None)
    assert queued["unavailable_reason"] == "command_store_unavailable"


# =============================================================================
# approvals (the owner approves every entry)
# =============================================================================


def test_a_pending_proposal_carries_what_the_approval_repeats(world: World) -> None:
    world.healthy()
    proposal = entry_proposal()
    body = world.today()
    [item] = body["approvals"]["items"]
    assert body["approvals"]["problem_count"] == 0
    assert item["approve_args"] == model_document(approve_command(proposal).args)
    assert item["proposal_id"] == proposal.proposal_id
    assert item["proposal_sha256"] == proposal_sha256(proposal)
    assert item["seconds_to_deadline"] == int((proposal.send_deadline - NOW).total_seconds())
    assert item["summary"] == fixture_keys("today.json")["approvals"]["items"][0]["summary"]
    assert (item["max_loss_cents"], item["notional_cents"]) == (
        proposal.review.max_loss_cents,
        proposal.review.notional_cents,
    )
    assert (item["asset_kind"], item["intent_id"], item["program_id"]) == (
        "equity",
        proposal.effect.intent_id,
        proposal.program_id,
    )
    assert item["precheck_blockers"] == []
    # Today agrees with the Wave 2 detail (GET /api/proposals) it summarizes
    [row] = proposals_payload(world.run_dir, world.supervised, NOW)["proposals"]
    assert (row["status"], row["proposal_sha256"], row["send_deadline"]) == (
        "pending",
        item["proposal_sha256"],
        item["send_deadline"],
    )


def test_an_option_proposal_is_summarized_by_its_structure(world: World) -> None:
    fixture = contract_fixture("proposal_option.json")
    option = entry_proposal(
        effect=SupervisedEffect.model_validate(fixture["effect"]),
        proposed_at=iso(ago(NOW, 60)),
        send_deadline=iso(shift_instant(NOW, 600)),
        review={"quote": {**fixture["review"]["quote"], "as_of": iso(ago(NOW, 61))}},
    )
    world.propose(option)
    [item] = world.today()["approvals"]["items"]
    assert item["asset_kind"] == "option"
    structure = option.effect.structure
    assert item["summary"].startswith(f"Buy {option.effect.quantity} {structure.underlying} ")
    assert str(option.effect.limit) in item["summary"]


def test_expired_rejected_and_approved_proposals_are_not_pending(world: World) -> None:
    proposal = world.propose(entry_proposal())
    world.now = proposal.send_deadline  # the deadline itself has passed
    assert world.today()["approvals"]["items"] == []
    world.now = NOW
    write_json(world.layout.proposal_rejected(proposal.proposal_id), {"rejected": True})
    assert world.today()["approvals"]["items"] == []


def test_a_proposal_from_an_old_owner_epoch_is_invalidated(world: World) -> None:
    world.healthy()
    proposal = entry_proposal()
    world.owner("owner-20261022T150000Z-9999")  # the desk owner restarted
    body = world.today()
    [item] = body["approvals"]["items"]
    assert item["approve_args"]["owner_epoch"] == EPOCH  # repeated verbatim
    assert "approval_epoch_mismatch" in item["precheck_blockers"]
    found = blocker(body, "approval_epoch_mismatch")
    assert found["scope"] == "approvals"
    deadlines = [e for e in body["next_events"]["items"] if e["kind"] == "approval_deadline"]
    assert proposal.proposal_id not in [e["subject"] for e in deadlines]


def test_a_proposal_of_the_live_epoch_is_approvable(world: World) -> None:
    world.healthy()
    body = world.today()
    [item] = body["approvals"]["items"]
    assert "approval_epoch_mismatch" not in item["precheck_blockers"]
    deadlines = [e for e in body["next_events"]["items"] if e["kind"] == "approval_deadline"]
    assert [(e["at"], e["subject"]) for e in deadlines] == [
        (item["send_deadline"], item["proposal_id"])
    ]


def test_an_unknown_owner_epoch_is_not_called_a_mismatch(world: World) -> None:
    world.healthy()
    (world.run_dir / "owner.json").unlink()
    [item] = world.today()["approvals"]["items"]
    assert "approval_epoch_mismatch" not in item["precheck_blockers"]


def test_prechecks_name_what_the_desk_would_refuse(world: World) -> None:
    world.beat(HEARTBEAT_STALE_S + 1)  # stale owner
    world.watch()
    world.owner()
    delayed = entry_proposal(
        review={
            "quote": {
                **contract_fixture("proposal_equity.json")["review"]["quote"],
                "market_data_type": "delayed",
            }
        }
    )
    world.propose(delayed)
    pause.pause_entries(world.run_dir, actor="loopback:operator", reason="", now=ago(NOW, 5))
    (world.run_dir / "HALT").touch()
    body = world.today()
    [item] = body["approvals"]["items"]
    portfolio = portfolio_payload(world.run_dir, NOW)
    # the Wave 2 books' own entry blockers (risk and reconciliation absent, paused)
    assert set(portfolio["entry_blockers"]) <= set(item["precheck_blockers"])
    assert {"entries_paused", "halt", "exit_owner_not_ready", "equity_quote_not_realtime"} <= set(
        item["precheck_blockers"]
    )
    assert len(item["precheck_blockers"]) == len(set(item["precheck_blockers"]))
    assert "equity_quote_not_realtime" in codes(body)


def test_an_equity_proposal_outside_the_entry_window_is_prechecked(world: World) -> None:
    world.healthy()
    world.now = datetime(2026, 10, 22, 13, 31, tzinfo=UTC)  # 09:31 ET: window opens 09:35
    assert equity_entry_window_blockers(world.now) == ("entry_window_closed",)
    [item] = world.today()["approvals"]["items"]
    assert "entry_window_closed" in item["precheck_blockers"]


def test_a_bad_proposal_file_is_a_problem_not_a_crash(world: World) -> None:
    world.healthy()
    folder = world.layout.proposals()
    (folder / ("f" * 64 + ".json")).write_text("{not a proposal")
    approvals = world.today()["approvals"]
    assert approvals["status"] == "ok"
    assert approvals["problem_count"] == 1
    assert len(approvals["items"]) == 1


@NOT_ROOT
def test_an_unreadable_proposals_dir_is_unavailable_not_empty(world: World) -> None:
    world.healthy()
    folder = world.layout.proposals()
    folder.chmod(0)
    try:
        body = world.today()
    finally:
        folder.chmod(0o755)
    approvals = body["approvals"]
    assert approvals == {
        "status": "unavailable",
        "unavailable_reason": "proposals_dir_unreadable",
        "items": None,
        "problem_count": None,
    }
    assert "proposals_dir_unreadable" in codes(body)


# =============================================================================
# positions (Wave 2 books, summarized as /api/portfolio serves them)
# =============================================================================


def test_positions_count_distinct_attributed_intents_and_risk_figures(world: World) -> None:
    world.holdings(age_s=60)
    snapshot = world.risk(age_s=30)
    world.reconciliation()
    positions = world.today()["positions"]
    counted = intents_by_kind(HOLDING_ROWS)
    assert positions["holdings"] == {
        "status": "ok",
        "unavailable_reason": None,
        "as_of": iso(ago(NOW, 60)),
        "age_s": 60,
        "option_positions": counted["OPT"],
        "equity_positions": counted["STK"],
    }
    assert positions["risk"] == {
        "status": "fresh",
        "unavailable_reason": None,
        "as_of": iso(ago(NOW, 30)),
        "age_s": 30,
        "open_loss_cents": snapshot["open_loss_cents"],
        "day_realized_loss_cents": snapshot["day_realized_loss_cents"],
        "settled_cash_cents": snapshot["cash_cents"],
        "outside_positions": len(snapshot["outside_positions"]),
    }
    # the same files as the Wave 2 detail page
    portfolio = portfolio_payload(world.run_dir, NOW)
    assert portfolio["risk"]["open_loss_cents"] == positions["risk"]["open_loss_cents"]
    assert portfolio["cash"]["settled_cash_cents"] == positions["risk"]["settled_cash_cents"]


def test_no_positions_is_zero_only_when_the_books_say_so(world: World) -> None:
    world.holdings(rows=[])
    holdings = world.today()["positions"]["holdings"]
    assert (holdings["option_positions"], holdings["equity_positions"]) == (0, 0)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("missing", "holdings_missing"),
        ("corrupt", "holdings_unreadable"),
        ("wrong-schema", "holdings_invalid"),
        ("other-account", "holdings_invalid"),
        ("future", "holdings_invalid"),
    ],
)
def test_unknown_holdings_are_unavailable_never_zero(world: World, setup: str, reason: str) -> None:
    path = world.layout.holdings()
    if setup == "corrupt":
        path.parent.mkdir(parents=True)
        path.write_text("{oops")
    elif setup == "wrong-schema":
        write_json(path, {"schema": "something-else/1"})
    elif setup == "other-account":
        rows = [{**HOLDING_ROWS[0], "account_id": "DU0000002"}]
        write_json(
            path,
            {
                "schema": "trex-holdings/1",
                "account_id": ACCOUNT,
                "as_of": iso(NOW),
                "cash_micros": 0,
                "holdings": rows,
            },
        )
    elif setup == "future":
        world.holdings(age_s=-120)
    body = world.today()
    holdings = body["positions"]["holdings"]
    assert holdings == {
        "status": "unavailable",
        "unavailable_reason": reason,
        "as_of": None,
        "age_s": None,
        "option_positions": None,
        "equity_positions": None,
    }


def test_a_wave2_reason_outside_the_today_vocabulary_maps_to_its_nearest(world: World) -> None:
    portfolio = portfolio_payload(world.run_dir, NOW)
    portfolio["holdings"] = {**portfolio["holdings"], "reason": "holdings_account_mismatch"}
    holdings = tv.positions_summary(portfolio).holdings
    assert (holdings.status, holdings.unavailable_reason) == ("unavailable", "account_mismatch")


def test_a_stale_risk_snapshot_keeps_its_figures_and_blocks_entries(world: World) -> None:
    world.risk(age_s=121)
    world.reconciliation()
    body = world.today()
    risk = body["positions"]["risk"]
    assert (risk["status"], risk["unavailable_reason"], risk["age_s"]) == ("stale", None, 121)
    assert risk["open_loss_cents"] is not None
    found = blocker(body, "risk_snapshot_stale")
    assert found["scope"] == "entries"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("missing", "risk_snapshot_missing"),
        ("broker-unavailable", "risk_snapshot_unavailable"),
        ("future", "risk_snapshot_stale"),
    ],
)
def test_an_unknown_risk_snapshot_has_no_figures(world: World, setup: str, reason: str) -> None:
    if setup == "broker-unavailable":
        world.risk(
            status="unavailable",
            open_loss_cents=None,
            day_realized_loss_cents=None,
            cash_cents=None,
            equity_gross_cents=None,
            outside_positions=[],
        )
    elif setup == "future":
        world.risk(age_s=-300)
    risk = world.today()["positions"]["risk"]
    assert risk == {
        "status": "unavailable",
        "unavailable_reason": reason,
        "as_of": None,
        "age_s": None,
        "open_loss_cents": None,
        "day_realized_loss_cents": None,
        "settled_cash_cents": None,
        "outside_positions": None,
    }


def test_a_dirty_reconciliation_is_an_entry_blocker(world: World) -> None:
    world.risk()
    document = contract_fixture("reconciliation_required.json")
    document["as_of"] = iso(ago(NOW, 10))
    write_json(world.layout.reconciliation(), document)
    found = blocker(world.today(), "reconciliation_required")
    assert (found["scope"], found["severity"]) == ("entries", "warning")


def test_a_live_account_in_the_books_is_critical(world: World) -> None:
    world.risk(account_id="U1234567")
    found = blocker(world.today(), "non_paper_account")
    assert found["severity"] == "critical"


@pytest.mark.parametrize(("age_s", "lock", "halt"), [(10, False, False), (10, True, True)])
def test_held_positions_without_a_running_exit_owner_are_unguarded(
    world: World, age_s: int, lock: bool, halt: bool
) -> None:
    world.holdings()
    world.beat(age_s)
    world.lock = lock
    if halt:
        (world.run_dir / "HALT").touch()
    found = blocker(world.today(), "exit_unguarded")
    assert (found["scope"], found["severity"]) == ("exits", "critical")


def test_no_positions_and_a_healthy_owner_are_not_unguarded(world: World) -> None:
    world.healthy()
    world.holdings(rows=[])
    assert "exit_unguarded" not in codes(world.today())


# =============================================================================
# acceptance (C3.4 summary; live is never authorized)
# =============================================================================


def test_no_acceptance_document_is_unavailable(world: World) -> None:
    body = world.today()
    assert body["acceptance"] == {
        "status": "unavailable",
        "unavailable_reason": "acceptance_missing",
        "computed_at": None,
        "verdict": None,
        "streak_sessions": None,
        "option_lifecycles": None,
        "equity_lifecycles": None,
        "drills_passed": None,
        "streak_required": acc.REQUIRED_STREAK_SESSIONS,
        "lifecycles_required": acc.REQUIRED_LIFECYCLES_PER_KIND,
        "drills_required": len(acc.DRILL_IDS),
        "live_authorized": False,
    }
    assert blocker(body, "acceptance_missing")["severity"] == "info"


def test_the_acceptance_line_is_the_verified_documents_summary(world: World) -> None:
    document = acceptance_document()
    world.accept(document)
    assert world.today()["acceptance"] == model_document(w.AcceptanceSummary.of(document))


def test_an_acceptance_document_cannot_be_verified_without_the_calendar(world: World) -> None:
    def broken() -> Any:
        raise CalendarIntegrityError("calendar checksum mismatch")

    world.accept(acceptance_document())
    world.calendar = broken
    acceptance = world.today()["acceptance"]
    assert (acceptance["status"], acceptance["unavailable_reason"]) == (
        "unavailable",
        "calendar_unreadable",
    )
    assert acceptance["verdict"] is None


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        ("garbage", "acceptance_invalid"),
        ("verdict", "acceptance_invalid"),
        ("calendar", "acceptance_calendar_mismatch"),
    ],
)
def test_an_unverifiable_acceptance_document_is_unavailable(
    world: World, tamper: str, reason: str
) -> None:
    path = w.AcceptanceDir(world.acceptance_dir).document()
    path.parent.mkdir(parents=True)
    document = model_document(acceptance_document())
    if tamper == "garbage":
        path.write_text("{")
    else:
        if tamper == "verdict":
            document["verdict"] = "met"
        else:
            document["calendar_sha256"] = "0" * 64
        path.write_text(json.dumps(document))
    acceptance = world.today()["acceptance"]
    assert (acceptance["status"], acceptance["unavailable_reason"]) == ("unavailable", reason)
    assert acceptance["live_authorized"] is False


# =============================================================================
# next events
# =============================================================================


def kinds_at(body: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e["kind"], e["at"]) for e in body["next_events"]["items"]]


def at_et(day: date, wall: time) -> datetime:
    return datetime.combine(day, wall, tzinfo=ET).astimezone(UTC)


def test_next_events_of_an_open_session_are_time_ordered(world: World) -> None:
    world.healthy()
    proposal = entry_proposal()
    today = NOW.astimezone(ET).date()
    _, window_closes = equity_entry_window(today)
    assert kinds_at(world.today()) == [
        ("approval_deadline", iso(proposal.send_deadline)),
        ("entry_window_close", iso(window_closes)),
        ("session_close", iso(CAL.session_close(today))),
        ("backup", iso(at_et(today, SCHEDULED_AT))),
    ]


def test_next_events_before_the_open_start_with_the_open(world: World) -> None:
    world.now = datetime(2026, 10, 22, 11, 0, tzinfo=UTC)
    today = world.now.astimezone(ET).date()
    opens, closes = equity_entry_window(today)
    assert kinds_at(world.today()) == [
        ("session_open", iso(CAL.session_open(today))),
        ("entry_window_open", iso(opens)),
        ("entry_window_close", iso(closes)),
        ("session_close", iso(CAL.session_close(today))),
        ("backup", iso(at_et(today, SCHEDULED_AT))),
    ]


def test_after_the_close_the_backup_and_the_next_session_follow(world: World) -> None:
    world.now = datetime(2026, 10, 22, 20, 10, tzinfo=UTC)  # 16:10 ET
    today = world.now.astimezone(ET).date()
    following = date(2026, 10, 23)
    opens, closes = equity_entry_window(following)
    assert kinds_at(world.today()) == [
        ("backup", iso(at_et(today, SCHEDULED_AT))),
        ("session_open", iso(CAL.session_open(following))),
        ("entry_window_open", iso(opens)),
        ("entry_window_close", iso(closes)),
        ("session_close", iso(CAL.session_close(following))),
    ]


def test_held_desk_positions_schedule_their_forced_and_time_stop_exits(world: World) -> None:
    equity_spec = contract_fixture("desk_spec_v2_equity.json")
    option_spec = contract_fixture("desk_spec_v1_option.json")
    option_spec["structure"]["exit_deadline"] = "2026-10-26"
    sid = option_spec["structure"]["id"]
    write_json(world.run_dir / "specs" / f"{equity_spec['intent_id']}.json", equity_spec)
    write_json(world.run_dir / "specs" / f"{sid}.json", option_spec)
    write_json(world.run_dir / "specs" / "old-closed.json", {"not": "read"})
    world.beat(
        structures={
            equity_spec["intent_id"]: {"status": "open"},
            sid: {"status": "exit_working"},
            "old-closed": {"status": "closed"},
        }
    )
    events = world.today()["next_events"]["items"]
    policy = equity_effect().exit_policy
    forced = forced_exit_at(deadline_session(policy), policy.forced_exit_et)
    time_stop = EngineConfig(DEFAULT_ENTRY_WINDOW).time_stop_time
    stop_at = at_et(date(2026, 10, 26), time_stop)
    exits = [(e["kind"], e["at"], e["subject"]) for e in events if e["subject"] is not None]
    assert exits == sorted(
        [
            ("forced_exit", iso(forced), equity_spec["intent_id"]),
            ("exit_deadline", iso(stop_at), option_spec["intent_id"]),
        ],
        key=lambda e: e[1],
    )


def test_an_unreadable_spec_of_a_held_position_makes_the_schedule_unknown(world: World) -> None:
    world.beat(structures={"eq-spy-001": {"status": "open"}})
    (world.run_dir / "specs").mkdir()
    (world.run_dir / "specs" / "eq-spy-001.json").write_text("{")
    body = world.today()
    events = body["next_events"]
    assert events == {
        "status": "unavailable",
        "unavailable_reason": "schedule_unavailable",
        "items": None,
    }
    assert "schedule_unavailable" in codes(body)


def test_host_timers_come_from_the_injected_reader(world: World) -> None:
    tomorrow = at_et(date(2026, 10, 23), time(8, 0))
    run = w.ScheduledEvent(
        at=tomorrow, kind="premarket_run", label="Premarket research run", subject=None
    )
    past = w.ScheduledEvent(
        at=ago(NOW, 60), kind="premarket_run", label="Earlier run", subject=None
    )
    world.timers = lambda: (past, run)
    events = world.today()["next_events"]["items"]
    premarket = [e for e in events if e["kind"] == "premarket_run"]
    assert premarket == [model_document(run)]


def test_a_failing_timer_reader_makes_the_schedule_unknown(world: World) -> None:
    def broken() -> Sequence[w.ScheduledEvent]:
        raise OSError("the timer status file is unreadable")

    world.timers = broken
    body = world.today()
    assert body["next_events"]["unavailable_reason"] == "schedule_unavailable"
    assert blocker(body, "schedule_unavailable")["scope"] == "display"


# =============================================================================
# guards on this lane's files
# =============================================================================

BANNED = frozenset({"timedelta", "toordinal", "fromordinal", "weekday"})


def test_the_module_does_no_naive_date_arithmetic() -> None:
    tree = ast.parse(MODULE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in BANNED, node.lineno
        if isinstance(node, ast.Attribute):
            assert node.attr not in BANNED, node.lineno
        if isinstance(node, ast.ImportFrom):
            assert all(a.name not in BANNED for a in node.names), node.lineno


@pytest.mark.parametrize("path", [MODULE, Path(__file__)], ids=lambda p: p.name)
def test_no_en_or_em_dashes(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for dash in (chr(0x2013), chr(0x2014)):
        assert dash not in text, f"{path.name} contains U+{ord(dash):04X}"


def test_the_route_constant_is_the_contracts() -> None:
    assert tv.TODAY_PATH == w.TODAY_PATH == "/api/today"
