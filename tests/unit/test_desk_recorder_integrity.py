"""Recorder integrity: the sessions the desk must not lose.

Four independent oracles, one per way a chain session is lost today:

* ``features/<D>.json`` used to be written ONLY as a side effect of the deal
  miner, so a missed mine slot cost a features day on top of the missed
  chain day (2026-09-29 has a chain and no features document for exactly
  that reason). The features step now has its own entry point + timer.
* the chain recorder's capture window used to end at 12:30 ET on D+1, while
  the next run's ``finalize_prior`` closes D as a permanent gap. 2026-09-23
  was lost that way, and nothing read the 38 rows it wrote into
  ``gaps.jsonl`` for five days.
* the "no repair" guarantee -- a closed session can never be re-entered and
  ``validate()`` never returns ``ok`` for a past session -- had no test at
  all, so nothing would have caught a change that quietly re-opened it.
* the gap alarm must be silent when clean, loud when a session is lost, and
  must not read the session the recorder is still working on.

Every store / state / paper path is pinned to tmp by the autouse fixture
(``paths.py`` resolves all of them at call time), and no network is used:
the recorder runs through an injected transport.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.fixtures.desk_cboe import chain_payload, encode
from tree_options.desk import gap_check, paths, store
from tree_options.desk.__main__ import run_cli
from tree_options.desk.sessions import cutoff_instant, first_session_after
from tree_options.time.calendar import StaticSessionCalendar
from tree_options.trex.alert_policy import DEFAULT_QUIET, NOTIFY_RETRY_S, QuietHours

ET = ZoneInfo("America/New_York")
DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "desk"

D = date(2026, 9, 22)  # Tuesday
D1 = date(2026, 9, 23)  # Wednesday
D2 = date(2026, 9, 24)  # Thursday
PREV = date(2026, 9, 21)  # Monday: the newest CLOSED session while the
# recorder is working on D. A run that recorded D closed PREV's books in its
# finalize_prior; D's own books are not closed until a run records D1.
ALARM_NOW = datetime(2026, 9, 23, 7, 5, tzinfo=ET)  # the unit's 07:05 slot

# CBOE writes the end-of-day snapshot overnight: 03:49 UTC observed on
# 2026-09-23, i.e. 23:49 ET on 2026-09-22 (the timer says the same). After
# that instant on D the vendor is serving D's file, and it keeps serving it
# until the session rolls at 16:15 ET on D+1.
CBOE_PUBLISH_ET = time(23, 49)
ROLL_ET = time(16, 15)
LATE_SLOT_FLOOR = time(11, 0)  # the last effective slot must be this late or later

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_ONCAL = re.compile(
    r"^OnCalendar=\s*(?P<days>\*|[A-Z][a-z]{2}(?:\.\.[A-Z][a-z]{2})?)"
    r"\s+\*-\*-\*\s+(?P<hh>\d\d):(?P<mm>\d\d):(?P<ss>\d\d)"
    r"(?:\s+(?P<tz>\S+))?\s*$"
)


@pytest.fixture()
def cal(static_calendar: StaticSessionCalendar) -> StaticSessionCalendar:
    return static_calendar


@pytest.fixture(autouse=True)
def _pinned_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DESK_STORE", str(tmp_path / "store"))
    monkeypatch.setenv("TREX_DESK_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("DESK_PAPER_DIR", str(tmp_path / "paper"))
    monkeypatch.setenv("TREX_NOTIFY_ENV", str(tmp_path / "no-notify.env"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DESK_REPO_ROOT", raising=False)


def _transport(bodies: dict[str, bytes | Exception], calls: list[str] | None = None):
    """url -> body by symbol; an Exception value raises."""

    def t(url: str, *, timeout: float = 30.0) -> tuple[int, bytes]:
        if calls is not None:
            calls.append(url)
        sym = url.rsplit("/", 1)[1].removesuffix(".json")
        body = bodies.get(sym)
        if body is None:
            return 404, b"not found"
        if isinstance(body, Exception):
            raise body
        return 200, body

    return t


def _record(session: date, bodies: dict[str, bytes | Exception], cal) -> store.RunSummary:
    return store.record_session(
        session,
        sorted(bodies),
        store=store.ChainStore(paths.store_root()),
        transport=_transport(bodies),
        clock=lambda: datetime(2026, 9, 24, 18, 0, tzinfo=ET),
        sleep=lambda _s: None,
        cal=cal,
    )


def _write_manifest(session: date, statuses: dict[str, str]) -> None:
    store.atomic_write_json(
        paths.store_root() / "manifest" / f"{session.isoformat()}.json",
        {
            "schema": store.MANIFEST_SCHEMA,
            "session": session.isoformat(),
            "symbols": {
                sym: {
                    "status": st,
                    "n": 1 if st in store.RECORDED else 0,
                    "raw_sha256": None,
                    "detail": "",
                    "at": "2026-09-24T18:00:00-04:00",
                }
                for sym, st in statuses.items()
            },
            "updated_at": "2026-09-24T18:00:00-04:00",
        },
    )


# ------------------------------------------------------------------ C5 (a)
# features/<D>.json without the miner


class TestFeaturesWithoutTheMiner:
    def test_features_document_from_a_recorded_session_with_no_miner_run(
        self, tmp_path: Path, cal: StaticSessionCalendar
    ) -> None:
        """The loss this pins: features/<D>.json was written only as a side
        effect of the deal miner, so a missed mine slot lost the features
        day too. The recorder alone must be enough."""
        bodies = {"KO": encode(chain_payload()), "PEP": encode(chain_payload("PEP"))}
        _record(D, bodies, cal)
        out = paths.store_root() / "features" / f"{D.isoformat()}.json"
        assert not out.exists(), "the recorder must not write features itself"
        assert not (paths.state_root() / "stages" / D.isoformat()).exists()

        rc = run_cli(
            ["features", "--idempotent", "--session", D.isoformat()],
            cal=cal,
            now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        )
        assert rc == 0
        doc = json.loads(out.read_text())
        assert doc["session"] == D.isoformat()
        assert set(doc["names"]) == {"KO", "PEP"}
        # still no miner: the whole point is that the miner is not involved
        assert not (paths.state_root() / "queue" / f"{D.isoformat()}.json").exists()

    def test_idempotent_is_a_no_op_the_second_time(self, cal: StaticSessionCalendar) -> None:
        """Exit 0 with NOTHING done. The document is byte-identical after a
        rewrite (every input is hashed, not stamped), so the oracle is the
        write itself: atomic_write_json renames a fresh temp file over the
        target, which changes the inode. A skipped run leaves it alone --
        without this, a daily timer would silently re-derive the same
        features document forever."""
        _record(D, {"KO": encode(chain_payload())}, cal)
        args = ["features", "--idempotent", "--session", D.isoformat()]
        now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
        assert run_cli(args, cal=cal, now=now) == 0
        out = paths.store_root() / "features" / f"{D.isoformat()}.json"
        first = out.stat()
        assert run_cli(args, cal=cal, now=now) == 0  # nothing to do
        second = out.stat()
        assert out.read_bytes() == out.read_bytes()
        assert (second.st_ino, second.st_mtime_ns) == (first.st_ino, first.st_mtime_ns), (
            "the second run rewrote the document instead of skipping it"
        )

    def test_waits_for_the_session_to_close_then_builds(self, cal: StaticSessionCalendar) -> None:
        """A half-recorded D must not be frozen into the document: exit 3
        while the manifest still has pending symbols, then build once the
        recorder closes it."""
        bodies = {"KO": encode(chain_payload())}
        _record(D, bodies, cal)
        # a symbol the recorder has not managed yet
        _write_manifest(D, {"KO": "ok", "PEP": "stale"})
        now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
        out = paths.store_root() / "features" / f"{D.isoformat()}.json"
        assert (
            run_cli(["features", "--idempotent", "--session", D.isoformat()], cal=cal, now=now) == 3
        )
        assert not out.exists()

        _write_manifest(D, {"KO": "ok", "PEP": "ok"})
        assert (
            run_cli(["features", "--idempotent", "--session", D.isoformat()], cal=cal, now=now) == 0
        )
        assert out.exists()

    def test_session_defaults_to_the_latest_completed_one(self, cal: StaticSessionCalendar) -> None:
        _record(D, {"KO": encode(chain_payload())}, cal)
        # 2026-09-23 12:00 UTC is before D1's 16:15 ET cutoff, so D is still
        # the latest completed session and must be the default target.
        rc = run_cli(
            ["features", "--idempotent"],
            cal=cal,
            now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        )
        assert rc == 0
        assert (paths.store_root() / "features" / f"{D.isoformat()}.json").exists()
        assert not (paths.store_root() / "features" / f"{D1.isoformat()}.json").exists()

    def test_miner_semantics_are_unchanged_without_the_flag(
        self, cal: StaticSessionCalendar
    ) -> None:
        """--idempotent is the timer's mode. Without it the documented
        exit codes hold: 1 when D has no chains at all."""
        _record(D, {"KO": encode(chain_payload())}, cal)
        _write_manifest(D, {"KO": "ok", "PEP": "stale"})
        now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
        assert run_cli(["features", "--session", D.isoformat()], cal=cal, now=now) == 0
        assert run_cli(["features", "--session", D1.isoformat()], cal=cal, now=now) == 1
        assert run_cli(["features", "--session", "2026-09-20"], cal=cal, now=now) == 2  # a Sunday


# ------------------------------------------------------------------ C5 (c)
# the no-repair guarantee


class TestClosedSessionsAreNeverRepaired:
    def test_finalize_prior_closes_a_gap_and_a_rerun_cannot_reopen_it(
        self, cal: StaticSessionCalendar
    ) -> None:
        """2026-09-23 was lost this way: the recorder had not managed KO by
        the time the next session started, finalize_prior closed it as
        `missing`, and the chain can never come back (CBOE only serves the
        prior session's snapshot, which it has by then replaced)."""
        chain = store.ChainStore(paths.store_root())
        # D: KO lands, PEP does not (the vendor is still publishing)
        _record(D, {"KO": encode(chain_payload()), "PEP": None}, cal)
        assert chain.read_manifest(D)["symbols"]["PEP"]["status"] != "ok"

        # D1: the recorder runs again, which closes D's books for good
        _record(D1, {"KO": encode(chain_payload("KO", session=D1.isoformat()))}, cal)
        closed = chain.read_manifest(D)["symbols"]["PEP"]
        assert closed["status"] == "missing"
        assert "never recorded" in closed["detail"]
        assert not chain.chain_path(D, "PEP").exists()

        # A later run for D tries again and is refused: the feed has moved on
        moved_on = encode(chain_payload("PEP", session=D2.isoformat()))
        summary = _record(D, {"PEP": moved_on}, cal)
        assert summary.results["PEP"].status == "missing"
        assert not chain.chain_path(D, "PEP").exists()
        assert chain.read_manifest(D)["symbols"]["PEP"]["status"] == "missing"
        assert any(
            json.loads(line)["sym"] == "PEP" and json.loads(line)["status"] == "missing"
            for line in chain.gaps_path.read_text().splitlines()
        )

    def test_validate_never_returns_ok_for_a_past_session(self, cal: StaticSessionCalendar) -> None:
        """The other half of the guarantee: even a perfect-looking payload
        cannot be admitted to a session the feed has already moved past, so
        there is nothing for a repair to key on."""
        from tree_options.desk.chains import parse_chain

        late = parse_chain(encode(chain_payload("KO", session=D2.isoformat())), "KO")
        verdict = store.validate(late, D, cal)
        assert verdict.status == "missing"
        assert verdict.status != "ok"
        assert D2.isoformat() in verdict.detail

        # and the same payload IS ok for the session it actually describes
        same = parse_chain(encode(chain_payload("KO", session=D.isoformat())), "KO")
        assert store.validate(same, D, cal).status == "ok"

    def test_a_session_no_run_ever_attempted_is_a_whole_day_gap(
        self, cal: StaticSessionCalendar
    ) -> None:
        chain = store.ChainStore(paths.store_root())
        _record(D, {"KO": encode(chain_payload())}, cal)
        _record(date(2026, 9, 25), {"KO": encode(chain_payload("KO", session="2026-09-25"))}, cal)
        doc = chain.read_manifest(D1)
        assert doc is not None and doc["symbols"] == {}
        assert "no recorder run" in doc["note"]


# ------------------------------------------------------------------ C5 (b)
# the recorder's capture window, read out of the unit file


def _calendar_slots(unit: Path) -> list[tuple[frozenset[int], time]]:
    """(weekdays the slot fires on, time of day) for every OnCalendar line."""
    slots: list[tuple[frozenset[int], time]] = []
    for line in unit.read_text().splitlines():
        line = line.strip()
        if not line.startswith("OnCalendar="):
            continue
        m = _ONCAL.match(line)
        assert m is not None, f"unparsable OnCalendar line: {line!r}"
        spec = m.group("days")
        if spec == "*":
            days = frozenset(range(7))
        elif ".." in spec:
            lo, hi = (WEEKDAYS.index(x) for x in spec.split(".."))
            days = frozenset(range(lo, hi + 1))
        else:
            days = frozenset({WEEKDAYS.index(spec)})
        assert m.group("ss") == "00", line
        assert m.group("tz") == "America/New_York", line
        slots.append((days, time(int(m.group("hh")), int(m.group("mm")))))
    return slots


def _effective_slots(
    slots: list[tuple[frozenset[int], time]], session: date, cal: StaticSessionCalendar
) -> list[datetime]:
    """Every firing of ``slots`` strictly between CBOE publishing session
    ``session``'s snapshot and that session's roll, as aware instants."""
    publish = datetime.combine(session, CBOE_PUBLISH_ET, tzinfo=ET)
    nxt = first_session_after(session, cal)
    assert nxt is not None
    roll = cutoff_instant(nxt)
    fired: list[datetime] = []
    day = publish.date()
    while day <= roll.date():
        for days, at in slots:
            if day.weekday() in days:
                moment = datetime.combine(day, at, tzinfo=ET)
                if publish < moment <= roll:
                    fired.append(moment)
        day += timedelta(days=1)
    return sorted(fired)


# sessions that cover a plain weekday roll, a Friday->Monday weekend roll and
# a run-up to an early-close holiday (the roll is a SESSION, not a date)
SAMPLE_SESSIONS = (
    date(2026, 9, 22),  # Tue -> Wed
    date(2026, 9, 25),  # Fri -> Mon
    date(2026, 11, 25),  # Wed -> Fri (early close next day)
    date(2026, 12, 23),  # Wed -> Thu (holiday-shortened week)
)


class TestRecorderCaptureWindow:
    def test_a_session_stays_reachable_until_it_rolls(self, cal: StaticSessionCalendar) -> None:
        """The structural invariant behind the 2026-09-23 loss: there is at
        least one recorder firing strictly between the vendor publishing D's
        snapshot and D's 16:15 roll. Without one, D is unreachable by
        construction and the next run closes it as a permanent gap."""
        slots = _calendar_slots(DEPLOY / "desk-chain.timer")
        assert slots, "desk-chain.timer has no OnCalendar line"
        for session in SAMPLE_SESSIONS:
            fired = _effective_slots(slots, session, cal)
            assert fired, f"no recorder slot can reach {session}"

    def test_the_last_slot_is_late_enough_to_be_a_real_second_chance(
        self, cal: StaticSessionCalendar
    ) -> None:
        """One early-morning shot is not enough: 06:30 can land while CBOE
        is still writing the file, and there is no second attempt before the
        roll. The last firing of the day has to be well clear of 16:15."""
        slots = _calendar_slots(DEPLOY / "desk-chain.timer")
        for session in SAMPLE_SESSIONS:
            fired = _effective_slots(slots, session, cal)
            assert fired[-1].time() >= LATE_SLOT_FLOOR, (
                f"{session}: the last effective slot is {fired[-1]:%H:%M} ET, too early to "
                f"be a second chance before the 16:15 roll"
            )

    def test_the_1230_late_publication_catchup_is_pinned(self, cal: StaticSessionCalendar) -> None:
        """The 12:30 slot is pinned by name, not only by the two invariants
        above: it is the catch-up for a snapshot published later than usual,
        and desk-mine.timer schedules its 13:00 slot against it. Deleting it
        must fail loudly here rather than silently degrade the window."""
        slots = _calendar_slots(DEPLOY / "desk-chain.timer")
        assert any(at == time(12, 30) and WEEKDAYS.index("Mon") in days for days, at in slots), (
            "the 12:30 ET late-publication catch-up slot is gone from desk-chain.timer"
        )
        mine = _calendar_slots(DEPLOY / "desk-mine.timer")
        assert any(at == time(13, 0) for _, at in mine), (
            "desk-mine.timer's 13:00 slot is scheduled against the chain recorder's "
            "12:30 slot; the two must be changed together"
        )
        for session in SAMPLE_SESSIONS:
            fired = _effective_slots(slots, session, cal)
            assert any(m.time() == time(12, 30) for m in fired), (
                f"{session}: no 12:30 ET firing reaches it"
            )

    def test_the_1430_last_chance_is_before_the_roll(self, cal: StaticSessionCalendar) -> None:
        """14:30 is the last firing of the day: it must be inside the
        window (or the session has no last chance at all) and it must be
        followed by the roll, which is what closes the books."""
        slots = _calendar_slots(DEPLOY / "desk-chain.timer")
        assert any(at == time(14, 30) for _, at in slots)
        for session in SAMPLE_SESSIONS:
            fired = _effective_slots(slots, session, cal)
            last = fired[-1]
            assert last.time() == time(14, 30), f"{session}: last firing is {last:%H:%M}"
            nxt = first_session_after(session, cal)
            assert nxt is not None and last < cutoff_instant(nxt)


# ------------------------------------------------------------------ C5 (d)
# the gap alarm


def _default_quiet() -> QuietHours:
    """The operator's DEFAULT quiet window, built the way alert_policy
    builds it from notify.env (22:00-07:00 America/Denver)."""
    lo, hi = DEFAULT_QUIET.split("-")
    return QuietHours(
        time(int(lo[:2]), int(lo[3:])),
        time(int(hi[:2]), int(hi[3:])),
        ZoneInfo("America/Denver"),
    )


def _gap_store(manifests: dict[date, dict[str, str] | None]) -> store.ChainStore:
    for session, statuses in manifests.items():
        if statuses is not None:
            _write_manifest(session, statuses)
    return store.ChainStore(paths.store_root())


class TestGapAlarm:
    def test_clean_manifest_exits_zero_and_says_nothing(self, cal: StaticSessionCalendar) -> None:
        chain = _gap_store({PREV: {"KO": "ok", "PEP": "exists", "XLF": "conflict"}})
        sent: list[tuple[str, str, str]] = []
        gap, rc = gap_check.check_once(
            None,
            chain,
            cal=cal,
            now=ALARM_NOW,
            notify=lambda t, m, p: sent.append((t, m, p)) or True,
        )
        assert rc == 0 and gap.clean
        assert gap.session == PREV
        assert sent == []

    def test_one_missing_symbol_exits_nonzero_and_names_session_and_symbol(
        self, cal: StaticSessionCalendar
    ) -> None:
        chain = _gap_store({PREV: {"KO": "ok", "XLF": "missing"}})
        sent: list[tuple[str, str, str]] = []
        gap, rc = gap_check.check_once(
            None,
            chain,
            cal=cal,
            now=ALARM_NOW,
            notify=lambda t, m, p: sent.append((t, m, p)) or True,
        )
        assert rc == 1 and not gap.clean
        assert gap.symbols == ("XLF",)
        assert len(sent) == 1
        title, message, _ = sent[0]
        assert PREV.isoformat() in title and PREV.isoformat() in message
        assert "XLF" in message
        for leak in ("http", "://", "ntfy"):
            assert leak not in message.lower(), "push text must carry no URLs"

    def test_every_out_of_recorded_status_is_reported(self, cal: StaticSessionCalendar) -> None:
        chain = _gap_store(
            {
                PREV: {
                    "KO": "ok",
                    "A": "missing",
                    "B": "error",
                    "C": "invalid",
                    "D2": "stale",
                    "E": "incomplete",
                }
            }
        )
        gap, rc = gap_check.check_once(None, chain, cal=cal, now=ALARM_NOW)
        assert rc == 1
        assert gap.symbols == ("A", "B", "C", "D2", "E")
        assert gap.status == gap_check.GAPS

    def test_the_newest_session_is_skipped_while_it_is_still_open(
        self, cal: StaticSessionCalendar
    ) -> None:
        """The two sessions the alarm must never confuse. At 07:05 ET on
        2026-09-23 the latest COMPLETED session is D (16:15 ET has passed),
        and the recorder is working on it right now: `stale` there means "CBOE
        has not published yet", the normal state of a young manifest, and
        alarming on it would buzz every single morning. D's books are only
        closed by the run that records D1, so the alarm reads PREV."""
        chain = _gap_store(
            {
                PREV: {"KO": "ok", "PEP": "ok"},
                D: {"KO": "ok", "PEP": "stale", "XLF": "incomplete"},
                D1: {"KO": "ok", "PEP": "missing", "XLF": "missing"},
            }
        )
        gap, rc = gap_check.check_once(None, chain, cal=cal, now=ALARM_NOW)
        assert gap.session == PREV
        assert rc == 0 and gap.clean

    def test_a_closed_session_with_no_manifest_is_a_gap(self, cal: StaticSessionCalendar) -> None:
        """finalize_prior stamps a manifest on every session it skips, so a
        closed session without one means the books were never closed at all.
        That is the whole session, not a symbol: it must still alarm."""
        chain = _gap_store({D1: {"KO": "ok"}})  # nothing for the closed session
        sent: list[tuple[str, str, str]] = []
        gap, rc = gap_check.check_once(
            None,
            chain,
            cal=cal,
            now=ALARM_NOW,
            notify=lambda t, m, p: sent.append((t, m, p)) or True,
        )
        assert rc == 1 and gap.unreadable and gap.session == PREV
        assert len(sent) == 1 and PREV.isoformat() in sent[0][1]

    def test_nothing_to_say_before_the_first_session_has_closed(
        self, cal: StaticSessionCalendar
    ) -> None:
        """A fresh install: the calendar has no completed session yet, so
        there is no book that could have been lost. The unit must not alarm
        on its own arrival."""
        first = cal.sessions()[0]
        gap, rc = gap_check.check_once(
            None,
            store.ChainStore(paths.store_root()),
            cal=cal,
            now=datetime.combine(first, time(7, 5), tzinfo=ET),
        )
        assert rc == 0 and gap.clean and gap.session is None

    def test_an_empty_store_reports_every_closed_session_as_lost(
        self, cal: StaticSessionCalendar
    ) -> None:
        """Once sessions have closed, a store with no manifest at all is the
        whole gap -- refusing to be quiet about it is the point."""
        gap, rc = gap_check.check_once(
            None, store.ChainStore(paths.store_root()), cal=cal, now=ALARM_NOW
        )
        assert rc == 1 and gap.unreadable and gap.session == PREV

    def test_a_persisting_gap_is_pushed_once_then_held_to_the_cadence(
        self, cal: StaticSessionCalendar, tmp_path: Path
    ) -> None:
        """Once per reminder, not once per run: the state file is what keeps
        a permanently lost session from turning into a daily duplicate."""
        state = tmp_path / "state" / gap_check.STATE_FILE
        chain = _gap_store({PREV: {"KO": "ok", "XLF": "missing"}})
        sent: list[tuple[str, str, str]] = []
        push = lambda t, m, p: sent.append((t, m, p)) or True  # noqa: E731
        now = datetime(2026, 9, 23, 9, 5, tzinfo=ET)

        assert gap_check.check_once(state, chain, cal=cal, now=now, notify=push)[1] == 1
        assert len(sent) == 1
        # same gap, a minute later: the cadence has not elapsed
        assert (
            gap_check.check_once(
                state, chain, cal=cal, now=now + timedelta(minutes=1), notify=push
            )[1]
            == 1
        )
        assert len(sent) == 1

    def test_a_failed_push_is_owed_not_swallowed(
        self, cal: StaticSessionCalendar, tmp_path: Path
    ) -> None:
        """An unconfigured or rejected ntfy must not book the reminder as
        sent -- the next slot owes it (alert_policy's NOTIFY_RETRY_S)."""
        state = tmp_path / "state" / gap_check.STATE_FILE
        chain = _gap_store({PREV: {"KO": "ok", "XLF": "missing"}})
        calls: list[int] = []
        now = datetime(2026, 9, 23, 9, 5, tzinfo=ET)
        gap_check.check_once(
            state, chain, cal=cal, now=now, notify=lambda *_: calls.append(0) or False
        )
        assert len(calls) == 1
        assert json.loads(state.read_text())["notify_failed_at"] is not None
        # inside NOTIFY_RETRY_S the policy holds the retry, it does not drop it
        gap_check.check_once(
            state,
            chain,
            cal=cal,
            now=now + timedelta(seconds=60),
            notify=lambda *_: calls.append(0) or True,
        )
        assert len(calls) == 1, "the retry window was ignored"
        gap_check.check_once(
            state,
            chain,
            cal=cal,
            now=now + timedelta(seconds=NOTIFY_RETRY_S + 1),
            notify=lambda *_: calls.append(0) or True,
        )
        assert len(calls) == 2, "the failed reminder was swallowed instead of retried"

    def test_a_dry_run_owes_the_push_and_writes_no_state(
        self, cal: StaticSessionCalendar, tmp_path: Path
    ) -> None:
        state = tmp_path / "state" / gap_check.STATE_FILE
        chain = _gap_store({PREV: {"KO": "missing"}})
        sent: list[str] = []
        _, rc = gap_check.check_once(
            state,
            chain,
            cal=cal,
            now=datetime(2026, 9, 23, 9, 5, tzinfo=ET),
            notify=lambda t, *_: sent.append(t) or True,
            dry_run=True,
        )
        assert rc == 1
        assert sent == []
        assert not state.exists()

    def test_quiet_hours_hold_the_push_and_the_next_tick_delivers(
        self, cal: StaticSessionCalendar, tmp_path: Path
    ) -> None:
        """07:05 ET is inside the default 22:00-07:00 America/Denver window,
        so the unit has a second slot: this is why a single daily slot would
        never notify at all."""
        quiet = _default_quiet()
        state = tmp_path / "state" / gap_check.STATE_FILE
        chain = _gap_store({PREV: {"KO": "missing"}})
        sent: list[str] = []
        push = lambda t, *_: sent.append(t) or True  # noqa: E731
        quiet_now = datetime(2026, 9, 23, 7, 5, tzinfo=ET)  # 05:05 MDT
        loud_now = datetime(2026, 9, 23, 9, 5, tzinfo=ET)  # 07:05 MDT
        assert (
            gap_check.check_once(
                state,
                chain,
                cal=cal,
                now=quiet_now,
                notify=push,
                urg=gap_check.Urgency("high", 3600, quiet=quiet.contains(quiet_now.timestamp())),
            )[1]
            == 1
        )
        assert sent == [], "the push escaped quiet hours"
        assert (
            gap_check.check_once(
                state,
                chain,
                cal=cal,
                now=loud_now,
                notify=push,
                urg=gap_check.Urgency("high", 3600, quiet=quiet.contains(loud_now.timestamp())),
            )[1]
            == 1
        )
        assert len(sent) == 1 and PREV.isoformat() in sent[0]

    def test_the_timer_has_a_slot_outside_the_default_quiet_window(self) -> None:
        """The alarm's own unit: if every slot of desk-gap-check.timer lands
        inside the default quiet hours, a gap is never delivered."""
        quiet = _default_quiet()
        slots = _calendar_slots(DEPLOY / "desk-gap-check.timer")
        assert slots, "desk-gap-check.timer has no OnCalendar line"
        # 07:05 ET is 05:05 MDT; the 09:05 ET slot is what actually delivers
        assert any(
            not quiet.contains(datetime(2026, 9, 23, h, m, tzinfo=ET).timestamp())
            for _, (h, m) in ((d, (t.hour, t.minute)) for d, t in slots)
        )
        assert any(at == time(9, 5) for _, at in slots)


# ------------------------------------------------------- the units themselves


def test_every_new_unit_pins_the_working_directory_and_imports_the_checkout() -> None:
    """On 2026-09-23 an unknown writer repointed the shared .venv's editable
    install at a worktree; the sibling units all defend against it and these
    two must too."""
    for name in ("desk-features.service", "desk-gap-check.service"):
        text = (DEPLOY / name).read_text()
        assert "WorkingDirectory=/home/alexk/documents/tree_options" in text, name
        assert "Environment=PYTHONPATH=/home/alexk/documents/tree_options/src" in text, name
        assert "Environment=UV_NO_SYNC=1" in text, name
        assert "Environment=DESK_STORE=" in text, name
        m = re.search(r"TimeoutStartSec=(\d+)", text)
        assert m is not None and int(m.group(1)) > 0, name


def test_the_features_timer_runs_before_the_roll_and_the_chain_timers_exist(
    cal: StaticSessionCalendar,
) -> None:
    for unit, want in (("desk-features.timer", time(14, 30)), ("desk-gap-check.timer", None)):
        slots = _calendar_slots(DEPLOY / unit)
        assert slots, unit
        if want is not None:
            assert any(at == want for _, at in slots), unit
    for session in SAMPLE_SESSIONS:
        assert _effective_slots(_calendar_slots(DEPLOY / "desk-features.timer"), session, cal)


def test_a_features_document_is_written_to_the_store_the_unit_names() -> None:
    """desk-chain.service takes the store default (DESK_STORE unset = the
    checkout's artifacts/desk-store). The features job must write into THAT
    store, not a second one: a document in the wrong store reads as a lost
    day to everything downstream."""
    chain = (DEPLOY / "desk-chain.service").read_text()
    features = (DEPLOY / "desk-features.service").read_text()
    assert "Environment=DESK_STORE=" not in chain, "the recorder pins it now; update this"
    workdir = re.search(r"WorkingDirectory=(\S+)", chain)
    assert workdir is not None
    assert f"Environment=DESK_STORE={workdir.group(1)}/artifacts/desk-store" in features


def test_the_features_unit_writes_only_the_document_and_the_command_lock() -> None:
    """ProtectSystem=strict + ProtectHome=read-only means the ReadWritePaths
    list IS the unit's write surface: the document and the per-command lock
    (<state>/locks/features.lock, taken by every desk CLI command)."""
    text = (DEPLOY / "desk-features.service").read_text()
    rw = re.search(r"^ReadWritePaths=(.*)$", text, re.MULTILINE)
    assert rw is not None
    assert set(rw.group(1).split()) == {
        "/home/alexk/documents/tree_options/artifacts/desk-store/features",
        "/home/alexk/.local/state/trex-desk/locks",
    }
    # the features step reads the store and the research lane, writes nothing
    # else, and must not reach the network or a push channel
    assert "tree_options.trex.notify" not in text
    assert "-m tree_options.desk features --idempotent" in text
