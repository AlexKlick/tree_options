"""The Wave 4 alert sweep over real desk files (lane J).

Each test builds a desk run dir, supervised dir and acceptance dir from the
frozen contract fixtures, runs one sweep with a recording sender and checks
what went out, what was booked, and that nothing sensitive is in the text.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tree_options.trex import alert_policy as ap
from tree_options.trex import contracts as c
from tree_options.trex.clock import session_calendar
from tree_options.trex.contracts import acceptance as acc
from tree_options.trex.contracts.canonical import model_bytes
from tree_options.trex.contracts.risk import Reconciliation
from tree_options.trex.reconcile import write_reconciliation

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests/contracts/fixtures"
MT = ZoneInfo("America/Denver")
ACCOUNT = "DU0000001"  # the fixtures' paper account id: never in a push


def Z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@dataclass
class Sender:
    ok: bool = True
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def __call__(self, title: str, message: str, priority: str) -> bool:
        self.calls.append((title, message, priority))
        return self.ok


@pytest.fixture
def desk(tmp_path: Path) -> Any:
    from tree_options.trex import alert_sweep as sw

    run, sup, acc_dir = tmp_path / "run", tmp_path / "sup", tmp_path / "acceptance"
    for d in (run, sup, acc_dir):
        d.mkdir()
    return sw.SweepPaths(
        run_dir=run,
        supervised_dir=sup,
        acceptance_dir=acc_dir,
        book=tmp_path / "state" / "wave4-alerts.json",
    )


def _sweep(paths: Any, now: datetime, send: Sender, **kw: Any) -> Any:
    from tree_options.trex import alert_sweep as sw

    return sw.sweep(paths, now=now, quiet=kw.pop("quiet", None), send=send, **kw)


def _book(paths: Any) -> dict[str, Any]:
    return json.loads(paths.book.read_text())


def _write_proposal(paths: Any, name: str = "proposal_equity.json") -> c.EntryProposal:
    proposal = c.EntryProposal.model_validate(_fixture(name))
    folder = paths.run_dir / "proposals"
    folder.mkdir(exist_ok=True)
    (folder / f"{proposal.proposal_id}.json").write_bytes(c.proposal_bytes(proposal))
    return proposal


# the equity fixture: proposed 14:00Z, send_deadline 14:10:00Z on Thu 2026-10-22
DEADLINE = Z("2026-10-22T14:10:00Z")


# -- approvals ---------------------------------------------------------------------


def test_an_expiring_approval_pushes_once_and_is_booked(desk: Any) -> None:
    proposal = _write_proposal(desk)
    send = Sender()
    early = _sweep(desk, Z("2026-10-22T14:08:59Z"), send)  # 61 s left
    assert send.calls == [] and early.pushed == []
    result = _sweep(desk, Z("2026-10-22T14:09:15Z"), send)  # 45 s left
    (title, message, priority) = send.calls[0]
    assert result.pushed == ["approval_expiring"] and priority == "high"
    assert "SPY" in message and "45 s" in message and "45 s" in title
    book = _book(desk)
    assert f"approval_expiring:{proposal.proposal_id}" in book["alerts"]
    _sweep(desk, Z("2026-10-22T14:09:40Z"), send)
    assert len(send.calls) == 1


def test_an_approved_rejected_or_expired_proposal_never_alerts(desk: Any) -> None:
    proposal = _write_proposal(desk)
    send = Sender()
    _sweep(desk, DEADLINE, send)  # expired at the deadline
    rejected = c.Wave2RunDir(desk.run_dir).proposal_rejected(proposal.proposal_id)
    rejected.write_text("{}")
    _sweep(desk, Z("2026-10-22T14:09:30Z"), send)
    rejected.unlink()
    mandates = desk.supervised_dir / "mandates"
    mandates.mkdir()
    mandate = _fixture("mandate_v2.json")
    assert mandate["proposal_id"] == proposal.proposal_id
    (mandates / f"{proposal.effect.intent_id}.json").write_text(json.dumps(mandate))
    _sweep(desk, Z("2026-10-22T14:09:30Z"), send)
    assert send.calls == []


def test_quiet_hours_hold_an_expiring_approval(desk: Any) -> None:
    _write_proposal(desk)
    send = Sender()
    morning = ap.QuietHours(time(7, 0), time(9, 0), MT)  # 08:09 MDT
    result = _sweep(desk, Z("2026-10-22T14:09:15Z"), send, quiet=morning)
    assert send.calls == [] and result.held == 1


def test_pending_reader_matches_the_web_proposals_view(desk: Any) -> None:
    """Same files, same answer as ``GET /api/proposals``: pending, approved,
    rejected, expired, unreadable and non-canonical."""
    from tree_options.trex import alert_sweep as sw
    from tree_options.trex_web.commands_view import proposals_payload

    equity = _write_proposal(desk)
    option = _write_proposal(desk, "proposal_option.json")
    folder = desk.run_dir / "proposals"
    (folder / ("e" * 64 + ".json")).write_text("{not json")
    raw = json.loads((folder / f"{option.proposal_id}.json").read_text())
    (folder / ("f" * 64 + ".json")).write_text(json.dumps(raw, indent=2))
    cases = [
        # both before their deadlines (the web does not look at proposed_at)
        (Z("2026-10-14T13:55:00Z"), {equity.proposal_id, option.proposal_id}),
        (Z("2026-10-22T14:05:00Z"), {equity.proposal_id}),  # option expired
        (DEADLINE, set()),  # both expired
    ]
    for now, by_hand in cases:
        web = proposals_payload(desk.run_dir, desk.supervised_dir, now)
        expected = {r["proposal_id"] for r in web["proposals"] if r["status"] == "pending"}
        got, problems = sw.pending_approvals(desk.run_dir, desk.supervised_dir, now)
        assert {p.proposal_id for p in got} == expected == by_hand, now
        assert problems == len(web["problems"]) == 2, now


def test_pending_reader_bounds_match_the_web() -> None:
    from tree_options.trex import alert_sweep as sw
    from tree_options.trex_web import commands_view

    assert sw.MAX_PROPOSALS == commands_view.MAX_PROPOSALS
    assert sw.MAX_PROPOSAL_BYTES == commands_view.MAX_PROPOSAL_BYTES


# -- reconciliation -----------------------------------------------------------------


def _write_reconciliation(paths: Any, status: str = "reconciliation_required") -> None:
    doc = _fixture("reconciliation_required.json")
    if status != "reconciliation_required":
        doc.update(status=status, mismatches=[])
    write_reconciliation(c.Wave2RunDir(paths.run_dir), Reconciliation.model_validate(doc))


def test_reconciliation_required_alerts_after_two_cycles_even_at_night(desk: Any) -> None:
    _write_reconciliation(desk)
    send = Sender()
    quiet = ap.QuietHours(time(22, 0), time(7, 0), MT)
    night = datetime(2026, 10, 23, 1, 0, tzinfo=MT)
    _sweep(desk, night, send, quiet=quiet)
    assert send.calls == []  # one cycle can be a fill not booked yet
    result = _sweep(desk, datetime(2026, 10, 23, 1, 2, tzinfo=MT), send, quiet=quiet)
    (title, message, priority) = send.calls[0]
    assert result.pushed == ["reconciliation_required"] and priority == "urgent"
    assert "reconciliation" in title.lower() and "1 position" in message
    assert ACCOUNT not in title + message


def test_clean_reconciliation_resolves_the_alert(desk: Any) -> None:
    _write_reconciliation(desk)
    send = Sender()
    _sweep(desk, Z("2026-10-22T14:10:00Z"), send)
    _sweep(desk, Z("2026-10-22T14:12:00Z"), send)
    assert len(send.calls) == 1
    _write_reconciliation(desk, "clean")
    _sweep(desk, Z("2026-10-22T14:13:00Z"), send)
    assert _book(desk)["alerts"] == {}


def test_an_unreadable_reconciliation_keeps_the_booked_alert(desk: Any) -> None:
    _write_reconciliation(desk)
    send = Sender()
    _sweep(desk, Z("2026-10-22T14:10:00Z"), send)
    _sweep(desk, Z("2026-10-22T14:12:00Z"), send)
    path = c.Wave2RunDir(desk.run_dir).reconciliation()
    good = path.read_bytes()
    path.write_text("{broken")
    blind = _sweep(desk, Z("2026-10-22T14:13:00Z"), send)
    assert blind.sources["reconciliation"] == "reconciliation_unreadable"
    assert "reconciliation_required:books" in _book(desk)["alerts"]
    path.write_bytes(good)
    _sweep(desk, Z("2026-10-22T14:14:00Z"), send)
    assert len(send.calls) == 1  # still the same booked occurrence


# -- acceptance ------------------------------------------------------------------------

OWNER = "owner@example.com"
CAL = session_calendar()


def _session(day: str, heartbeat: int = 390) -> acc.SessionRecord:
    return acc.SessionRecord.model_validate(
        {
            "session": day,
            "heartbeat_minutes": heartbeat,
            "reconciliation_open": "clean",
            "reconciliation_close": "clean",
            "reconciliation_episodes": [],
            "unowned_orders": 0,
            "max_exit_unguarded_s": 0,
            "unavailable": {},
            "evidence": [],
        }
    )


def _write_acceptance(paths: Any, sessions: list[acc.SessionRecord], at: datetime) -> None:
    doc = acc.compute_acceptance(
        sessions=sessions,
        lifecycles=[],
        drills=[],
        owners=[OWNER],
        calendar=CAL,
        computed_at=at,
    )
    paths.acceptance_dir.joinpath("acceptance.json").write_bytes(model_bytes(doc))


# Mon 2026-11-02 .. Thu 2026-11-05 are sessions; Thu closes 21:00Z
AFTER_NOV5 = Z("2026-11-05T22:00:00Z")


def test_a_broken_streak_alerts_once(desk: Any) -> None:
    days = ["2026-11-02", "2026-11-03", "2026-11-04"]
    _write_acceptance(desk, [*map(_session, days), _session("2026-11-05", 300)], AFTER_NOV5)
    send = Sender()
    result = _sweep(desk, AFTER_NOV5, send)
    (title, message, priority) = send.calls[0]
    assert result.pushed == ["acceptance_streak_broken"] and priority == "default"
    assert "streak" in title and "2026-11-05" in message and "3" in message
    _sweep(desk, Z("2026-11-05T22:01:00Z"), send)
    assert len(send.calls) == 1


def test_a_running_streak_is_quiet(desk: Any) -> None:
    days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]
    _write_acceptance(desk, [_session(d) for d in days], AFTER_NOV5)
    send = Sender()
    result = _sweep(desk, AFTER_NOV5, send)
    assert send.calls == [] and result.sources["acceptance"] == "ok"


@pytest.mark.parametrize(
    ("content", "reason"),
    [(None, "acceptance_missing"), (b"{not json", "acceptance_invalid")],
)
def test_an_unusable_acceptance_document_is_unavailable_not_an_alert(
    desk: Any, content: bytes | None, reason: str
) -> None:
    if content is not None:
        desk.acceptance_dir.joinpath("acceptance.json").write_bytes(content)
    send = Sender()
    result = _sweep(desk, AFTER_NOV5, send)
    assert send.calls == [] and result.sources["acceptance"] == reason


def test_an_acceptance_document_for_another_calendar_is_refused(desk: Any) -> None:
    days = ["2026-11-02", "2026-11-03", "2026-11-04"]
    _write_acceptance(desk, [*map(_session, days), _session("2026-11-05", 300)], AFTER_NOV5)
    path = desk.acceptance_dir / "acceptance.json"
    doc = json.loads(path.read_text())
    doc["calendar_sha256"] = "0" * 64
    path.write_text(json.dumps(doc))
    send = Sender()
    result = _sweep(desk, AFTER_NOV5, send)
    assert send.calls == [] and result.sources["acceptance"] == "acceptance_calendar_mismatch"


def test_a_document_naming_other_owners_or_from_the_future_never_alerts(
    desk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration fix (review P2): the sweep refuses what GET /api/acceptance
    refuses (an owner list other than TREX_OWNER_EMAILS, a future date)."""
    days = ["2026-11-02", "2026-11-03", "2026-11-04"]
    _write_acceptance(desk, [*map(_session, days), _session("2026-11-05", 300)], AFTER_NOV5)
    send = Sender()
    monkeypatch.setenv("TREX_OWNER_EMAILS", "mallory@example.net")
    result = _sweep(desk, AFTER_NOV5, send)
    assert send.calls == [] and result.sources["acceptance"] == "acceptance_owners_mismatch"
    monkeypatch.setenv("TREX_OWNER_EMAILS", OWNER)
    early = _sweep(desk, Z("2026-11-05T21:54:59Z"), send)  # computed 22:00Z: 301 s ahead
    assert send.calls == [] and early.sources["acceptance"] == "acceptance_invalid"
    result = _sweep(desk, AFTER_NOV5, send)
    assert result.pushed == ["acceptance_streak_broken"]


def test_the_future_skew_matches_the_web_view() -> None:
    from tree_options.trex import alert_sweep as sw
    from tree_options.trex_web import acceptance_view

    assert sw.ACCEPTANCE_FUTURE_SKEW_S == acceptance_view.FUTURE_SKEW_S


# -- exit_quote_blind ------------------------------------------------------------------


def _event(**over: Any) -> str:
    event = {
        "ts": "2026-10-22T10:00:00-04:00",
        "event": "exit_quote_blind",
        "structure": "eq-spy-001",
        "symbol": "SPY",
        "reason": "forced",
        "session": "2026-10-22",
        "blind_since": "2026-10-22T09:55:00-04:00",
        "blind_s": 300,
        "after_s": 300,
    }
    event.update(over)
    return json.dumps(event, sort_keys=True) + "\n"


def test_exit_quote_blind_goes_out_even_in_quiet_hours(desk: Any) -> None:
    (desk.run_dir / "events.jsonl").write_text(_event())
    send = Sender()
    session_quiet = ap.QuietHours(time(7, 0), time(9, 0), MT)  # 08:00 MDT = 10:00 ET
    result = _sweep(desk, Z("2026-10-22T14:00:30Z"), send, quiet=session_quiet)
    (title, message, priority) = send.calls[0]
    assert result.pushed == ["exit_quote_blind"] and priority == "urgent"
    assert "SPY" in title + message


def test_only_the_tail_of_a_long_event_log_is_read(desk: Any) -> None:
    from tree_options.trex import alert_sweep as sw

    filler = json.dumps({"event": "heartbeat", "pad": "x" * 200}) + "\n"
    old = _event(structure="eq-old-1", symbol="QQQ")
    count = sw.EVENTS_TAIL_BYTES // len(filler) + 10
    (desk.run_dir / "events.jsonl").write_text(old + filler * count + _event())
    send = Sender()
    _sweep(desk, Z("2026-10-22T14:00:30Z"), send)
    assert [call[0] for call in send.calls] == ["trex: exit quote blind (SPY)"]


def test_the_events_file_is_the_desk_runtime_one() -> None:
    from tree_options.trex import alert_sweep as sw
    from tree_options.trex.desk_runtime import DeskPaths

    assert DeskPaths(Path("/x")).events() == Path("/x") / sw.DESK_EVENTS


# -- delivery ----------------------------------------------------------------------------


def test_a_failed_push_is_owed_and_retried_after_the_backoff(desk: Any) -> None:
    (desk.run_dir / "events.jsonl").write_text(_event())
    down = Sender(ok=False)
    first = _sweep(desk, Z("2026-10-22T14:00:30Z"), down)
    assert first.failed == 1 and first.degraded is True
    entry = _book(desk)["alerts"]["exit_quote_blind:eq-spy-001:2026-10-22"]
    assert entry["last_notified_at"] is None and entry["notify_failed_at"] is not None
    _sweep(desk, Z("2026-10-22T14:03:00Z"), down)
    assert len(down.calls) == 1  # inside the 5-minute backoff
    up = Sender()
    _sweep(desk, Z("2026-10-22T14:05:30Z"), up)
    assert len(up.calls) == 1


def test_a_raising_sender_counts_as_a_failed_push(desk: Any) -> None:
    (desk.run_dir / "events.jsonl").write_text(_event())

    def boom(title: str, message: str, priority: str) -> bool:
        raise OSError("network down")

    result = _sweep(desk, Z("2026-10-22T14:00:30Z"), boom)  # type: ignore[arg-type]
    assert result.failed == 1


def test_dry_run_sends_and_writes_nothing(desk: Any) -> None:
    (desk.run_dir / "events.jsonl").write_text(_event())
    send = Sender()
    result = _sweep(desk, Z("2026-10-22T14:00:30Z"), send, dry_run=True)
    assert send.calls == [] and not desk.book.exists()
    assert result.planned == ["exit_quote_blind"]


def test_a_corrupt_book_starts_over_instead_of_failing(desk: Any) -> None:
    desk.book.parent.mkdir(parents=True)
    desk.book.write_text("{corrupt")
    (desk.run_dir / "events.jsonl").write_text(_event())
    send = Sender()
    _sweep(desk, Z("2026-10-22T14:00:30Z"), send)
    assert len(send.calls) == 1 and _book(desk)["schema"] == "trex-alert-book/1"


def test_the_book_file_is_private(desk: Any) -> None:
    (desk.run_dir / "events.jsonl").write_text(_event())
    _sweep(desk, Z("2026-10-22T14:00:30Z"), Sender())
    assert desk.book.stat().st_mode & 0o077 == 0


def test_no_push_carries_account_data_urls_or_money(desk: Any) -> None:
    _write_proposal(desk)
    _write_reconciliation(desk)
    (desk.run_dir / "events.jsonl").write_text(_event())
    send = Sender()
    _sweep(desk, Z("2026-10-22T14:07:15Z"), send)
    _sweep(desk, Z("2026-10-22T14:09:15Z"), send)
    kinds = {call[0] for call in send.calls}
    assert len(send.calls) == 3, kinds
    for title, message, _ in send.calls:
        text = title + message
        for banned in (ACCOUNT, "http", "$", "@", ".ts.net", "cents"):
            assert banned not in text, (banned, text)


def test_sources_report_every_input(desk: Any) -> None:
    result = _sweep(desk, Z("2026-10-22T14:00:30Z"), Sender())
    assert result.sources == {
        "reconciliation": "reconciliation_missing",
        "proposals": "ok",
        "acceptance": "acceptance_missing",
        "desk_events": "ok",
    }
    assert result.degraded is False


def test_the_sweep_loads_no_broker_or_web_library() -> None:
    probe = (
        "import sys, tree_options.trex.alert_sweep, tree_options.trex.deadman;"
        " sys.exit(1 if 'ib_async' in sys.modules or 'fastapi' in sys.modules else 0)"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe], cwd=REPO, env=env, capture_output=True, timeout=120
    )
    assert done.returncode == 0, done.stderr.decode()[-2000:]


def test_now_must_be_timezone_aware(desk: Any) -> None:
    with pytest.raises(ValueError):
        _sweep(desk, datetime(2026, 10, 22, 14, 0), Sender())


def test_the_sweep_runs_on_utc_instants(desk: Any) -> None:
    """The same instant in another zone is the same sweep."""
    _write_proposal(desk)
    send = Sender()
    _sweep(desk, Z("2026-10-22T14:09:15Z").astimezone(MT), send)
    assert len(send.calls) == 1
    assert Z("2026-10-22T14:09:15Z").astimezone(UTC) == Z("2026-10-22T14:09:15Z")
