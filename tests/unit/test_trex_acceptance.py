"""Wave 4 lane H: the acceptance recorder and compute job (``trex.acceptance``).

Oracles are hand counts against the repo NYSE calendar (November 2026 is
EST: the open is 14:30Z, the close 21:00Z, 390 minutes; 2026-10-22 is EDT:
13:30Z to 20:00Z) and the frozen contract (``contracts.acceptance``), never
this lane's own derivation. Samples are built literally; every expected
instant below is written out by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.trex_fakes import entry_proposal, equity_effect, ledger_event, mandate_v2
from tree_options.time.sessions import shift_instant
from tree_options.trex import acceptance as acc
from tree_options.trex import exit_watch
from tree_options.trex.clock import session_calendar
from tree_options.trex.contracts import acceptance as contract
from tree_options.trex.contracts.canonical import model_bytes, model_document
from tree_options.trex.contracts.proposals import proposal_bytes
from tree_options.trex.contracts.risk import PositionMismatch, Reconciliation
from tree_options.trex.contracts.wave3 import AcceptanceDir
from tree_options.trex.engine import ExitReason
from tree_options.trex.equity_exits import EquityExitReason

REPO = Path(__file__).resolve().parents[2]
CAL = session_calendar()
OWNER = "owner@example.com"
NOV16 = date(2026, 11, 16)
NOV17 = date(2026, 11, 17)
NOV18 = date(2026, 11, 18)


def Z(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def seen(
    status: str = "clean", as_of: str | datetime = "", mismatches=()
) -> acc.ReconciliationSeen:
    return acc.ReconciliationSeen(
        status=status,
        as_of=Z(as_of) if isinstance(as_of, str) else as_of,
        sha256="a" * 64,
        mismatch_con_ids=tuple(mismatches),
    )


def sample(
    at: datetime,
    *,
    beat_age: int | None = 10,
    exposed: bool | None = False,
    halt: bool | None = False,
    rec: str | None = "clean",
    rec_age: int = 20,
    rec_as_of: datetime | None = None,
    mismatches=(),
    gateway: str | None = "ok",
) -> acc.Sample:
    """One recorder sample; ``beat_age`` None = book unreadable."""
    book_ok = beat_age is not None
    return acc.Sample(
        at=at,
        heartbeat_at=shift_instant(at, -beat_age) if book_ok else None,
        heartbeat_unavailable_reason=None if book_ok else "book_unreadable",
        exposed=exposed if book_ok else None,
        exposed_unavailable_reason=None if book_ok else "book_unreadable",
        halt=halt,
        halt_unavailable_reason=None if halt is not None else "halt_unreadable",
        reconciliation=(
            None if rec is None else seen(rec, rec_as_of or shift_instant(at, -rec_age), mismatches)
        ),
        reconciliation_unavailable_reason=None if rec is not None else "reconciliation_missing",
        gateway=None if gateway is None else acc.GatewaySeen(status=gateway, checked_at=at),
        gateway_unavailable_reason=None if gateway is not None else "gateway_watch_not_reporting",
    )


def every_30s(day: date, **kw: Any) -> list[acc.Sample]:
    """Samples at open + 30 k seconds over the whole session (two a minute)."""
    cal = acc.calendar_session(CAL, day)
    n = cal.regular_minutes * 2
    return [sample(shift_instant(cal.opens_at, 30 * k), **kw) for k in range(n)]


def replace_minutes(samples, day: date, minutes: range, **kw: Any) -> list[acc.Sample]:
    """Rebuild the samples whose minute (since the open) is in ``minutes``."""
    opens = acc.calendar_session(CAL, day).opens_at
    out = []
    for s in samples:
        minute = int((s.at - opens).total_seconds()) // 60
        out.append(sample(s.at, **kw) if minute in minutes else s)
    return out


# =============================================================================
# pinned constants: the recorder judges by the exit watch's own thresholds
# =============================================================================


def test_constants_are_the_exit_watch_and_contract_ones() -> None:
    assert acc.HEARTBEAT_STALE_S == exit_watch.HEARTBEAT_STALE_S == 120
    assert acc.EXPOSED_STATUSES == exit_watch.EXPOSED
    assert acc.POLICY_EXIT_REASONS == (
        {r.value for r in ExitReason} | {r.value for r in EquityExitReason}
    ) - {"flatten"}
    assert acc.DEFAULT_ACCEPTANCE_DIR == "~/.local/state/trex/acceptance"


def test_acceptance_dir_from_env(tmp_path) -> None:
    assert acc.acceptance_dir_from_env({"TREX_ACCEPTANCE_DIR": str(tmp_path)}) == tmp_path
    assert acc.acceptance_dir_from_env({}) == Path("~/.local/state/trex/acceptance").expanduser()


# =============================================================================
# the sampler (read-only over the desk's files)
# =============================================================================


@pytest.fixture
def sources(tmp_path) -> acc.Sources:
    run = tmp_path / "desk-paper"
    (run / "ledger").mkdir(parents=True)
    sup = tmp_path / "supervised"
    (sup / "mandates").mkdir(parents=True)
    return acc.Sources(run_dir=run, supervised_dir=sup, gateway_state=tmp_path / "gateway.json")


def write_book(run: Path, heartbeat: object, statuses: dict[str, str]) -> None:
    doc = {"heartbeat": heartbeat, "structures": {k: {"status": v} for k, v in statuses.items()}}
    (run / "book.json").write_text(json.dumps(doc))


def write_reconciliation(run: Path, rec: Reconciliation) -> None:
    (run / "ledger" / "reconciliation.json").write_bytes(model_bytes(rec))


def test_sample_reads_heartbeat_exposure_halt_reconciliation_and_gateway(sources) -> None:
    now = Z("2026-11-16T15:00:00Z")
    write_book(sources.run_dir, "2026-11-16T09:59:50-05:00", {"a": "closed", "b": "open"})
    rec = Reconciliation(account_id="DU0000001", status="clean", as_of=Z("2026-11-16T14:59:30Z"))
    write_reconciliation(sources.run_dir, rec)
    sources.gateway_state.write_text(json.dumps({"status": "ok", "checked_at": now.timestamp()}))
    got = acc.take_sample(sources, now)
    assert got.at == now
    assert got.heartbeat_at == Z("2026-11-16T14:59:50Z")
    assert got.exposed is True and got.halt is False
    assert got.reconciliation is not None
    assert got.reconciliation.status == "clean"
    assert got.reconciliation.as_of == Z("2026-11-16T14:59:30Z")
    assert got.reconciliation.sha256 == hashlib.sha256(model_bytes(rec)).hexdigest()
    assert got.reconciliation.mismatch_con_ids == ()
    assert got.gateway == acc.GatewaySeen(status="ok", checked_at=now)
    reasons = (
        got.heartbeat_unavailable_reason,
        got.exposed_unavailable_reason,
        got.halt_unavailable_reason,
        got.reconciliation_unavailable_reason,
        got.gateway_unavailable_reason,
    )
    assert reasons == (None,) * 5


def test_sample_names_every_unknown_with_its_reason(sources) -> None:
    now = Z("2026-11-16T15:00:00Z")
    got = acc.take_sample(sources, now)
    assert (got.heartbeat_at, got.heartbeat_unavailable_reason) == (None, "book_missing")
    assert (got.exposed, got.exposed_unavailable_reason) == (None, "book_missing")
    assert (got.reconciliation, got.reconciliation_unavailable_reason) == (
        None,
        "reconciliation_missing",
    )
    assert (got.gateway, got.gateway_unavailable_reason) == (None, "gateway_watch_not_reporting")
    assert got.halt is False  # no HALT file: a known False


def test_sample_book_without_heartbeat_still_reads_exposure(sources) -> None:
    write_book(sources.run_dir, None, {"a": "exit_working"})
    (sources.run_dir / "HALT").write_text("{}")
    (sources.run_dir / "ledger" / "reconciliation.json").write_text("{not json")
    got = acc.take_sample(sources, Z("2026-11-16T15:00:00Z"))
    assert (got.heartbeat_at, got.heartbeat_unavailable_reason) == (None, "heartbeat_absent")
    assert got.exposed is True and got.halt is True
    assert got.reconciliation_unavailable_reason == "reconciliation_unreadable"


def test_sample_refuses_naive_heartbeat_and_foreign_reconciliation(sources) -> None:
    write_book(sources.run_dir, "2026-11-16T09:59:50", {})
    (sources.run_dir / "ledger" / "reconciliation.json").write_text('{"schema": "x"}')
    got = acc.take_sample(sources, Z("2026-11-16T15:00:00Z"))
    assert got.heartbeat_unavailable_reason == "heartbeat_invalid"
    assert got.exposed is False
    assert got.reconciliation_unavailable_reason == "reconciliation_invalid"


def test_unreadable_book_is_unknown_exposure(sources) -> None:
    (sources.run_dir / "book.json").write_text("[1, 2]")
    got = acc.take_sample(sources, Z("2026-11-16T15:00:00Z"))
    assert (got.exposed, got.exposed_unavailable_reason) == (None, "book_invalid")


def test_sample_mismatch_con_ids_come_from_the_document(sources) -> None:
    rec = Reconciliation(
        account_id="DU0000001",
        status="reconciliation_required",
        as_of=Z("2026-11-16T14:59:30Z"),
        mismatches=(PositionMismatch(con_id=756733, local_qty=2, broker_qty=0, kind="missing"),),
    )
    write_reconciliation(sources.run_dir, rec)
    got = acc.take_sample(sources, Z("2026-11-16T15:00:00Z"))
    assert got.reconciliation is not None
    assert got.reconciliation.mismatch_con_ids == (756733,)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-11-16T13:30:00Z", True),  # open 14:30Z minus one hour
        ("2026-11-16T13:29:59Z", False),
        ("2026-11-16T22:00:00Z", True),  # close 21:00Z plus one hour
        ("2026-11-16T22:00:01Z", False),
        ("2026-11-21T15:00:00Z", False),  # a Saturday
        ("2026-11-26T15:00:00Z", False),  # Thanksgiving
    ],
)
def test_sampling_window(instant: str, expected: bool) -> None:
    assert acc.should_sample(CAL, Z(instant)) is expected


def test_samples_append_to_the_et_date_file_and_read_back(tmp_path) -> None:
    adir = AcceptanceDir(tmp_path / "acc")
    first = sample(Z("2026-11-17T04:30:00Z"))  # 23:30 ET on 2026-11-16
    second = sample(Z("2026-11-17T05:30:00Z"))  # 00:30 ET on 2026-11-17
    p1 = acc.append_sample(adir, first)
    p2 = acc.append_sample(adir, second)
    p3 = acc.append_sample(adir, second)
    assert p1 == tmp_path / "acc/evidence/samples/2026-11-16.jsonl"
    assert p2 == p3 == tmp_path / "acc/evidence/samples/2026-11-17.jsonl"
    with p2.open("ab") as fh:
        fh.write(b'{"torn": ')  # a crash mid-line is one lost sample, nothing else
    got = acc.read_samples(p2)
    assert got.samples == (second, second)
    assert got.invalid_lines == 1
    assert got.sha256 == hashlib.sha256(p2.read_bytes()).hexdigest()
    assert acc.read_samples(p1).samples == (first,)


# =============================================================================
# per-session metrics (hand counts, 2026-11-16: 14:30Z to 21:00Z, 390 minutes)
# =============================================================================


def test_heartbeat_minutes_counts_fresh_sampled_minutes() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    base = every_30s(NOV16)
    assert acc.heartbeat_minutes(cal, base) == 390
    # five minutes with a 200 s old heartbeat: 385 of 390 (below 99%)
    assert (
        acc.heartbeat_minutes(cal, replace_minutes(base, NOV16, range(100, 105), beat_age=200))
        == 385
    )
    # exactly 120 s old is still fresh; 121 s is not
    assert (
        acc.heartbeat_minutes(cal, replace_minutes(base, NOV16, range(7, 9), beat_age=120)) == 390
    )
    assert (
        acc.heartbeat_minutes(cal, replace_minutes(base, NOV16, range(7, 9), beat_age=121)) == 388
    )
    # an unreadable book is not a fresh heartbeat
    assert (
        acc.heartbeat_minutes(cal, replace_minutes(base, NOV16, range(3, 4), beat_age=None)) == 389
    )


def test_heartbeat_minute_needs_every_sample_fresh_and_at_least_one() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    base = every_30s(NOV16)
    # minute 50: 15:20:00Z fresh, 15:20:30Z stale: the minute does not count
    mixed = [sample(s.at, beat_age=500) if s.at == Z("2026-11-16T15:20:30Z") else s for s in base]
    assert acc.heartbeat_minutes(cal, mixed) == 389
    # minutes 200 and 201 unsampled (17:50:00Z to 17:51:59Z): unknown never counts
    gap = [s for s in base if not Z("2026-11-16T17:50:00Z") <= s.at < Z("2026-11-16T17:52:00Z")]
    assert acc.heartbeat_minutes(cal, gap) == 388
    # samples outside the regular session add nothing
    extra = [sample(Z("2026-11-16T14:29:30Z")), sample(Z("2026-11-16T21:00:00Z"))]
    assert acc.heartbeat_minutes(cal, base + extra) == 390
    assert acc.heartbeat_minutes(cal, []) == 0


def test_heartbeat_minutes_on_an_early_close() -> None:
    cal = acc.calendar_session(CAL, date(2026, 11, 27))
    assert (cal.opens_at, cal.closes_at) == (Z("2026-11-27T14:30:00Z"), Z("2026-11-27T18:00:00Z"))
    assert acc.heartbeat_minutes(cal, every_30s(date(2026, 11, 27))) == 210


def test_reconciliation_clean_all_session() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    got = acc.reconciliation_summary(cal, every_30s(NOV16))
    assert (got.open, got.close, got.episodes) == ("clean", "clean", ())


def test_reconciliation_episode_starts_and_resolves_at_document_instants() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    # minutes 60..69 (15:30:00Z to 15:39:30Z) see a required document 20 s old
    samples = replace_minutes(every_30s(NOV16), NOV16, range(60, 70), rec="reconciliation_required")
    got = acc.reconciliation_summary(cal, samples)
    assert got.episodes == (
        contract.ReconciliationEpisode(
            started_at=Z("2026-11-16T15:29:40Z"),
            status="reconciliation_required",
            resolved_at=Z("2026-11-16T15:39:40Z"),
        ),
    )
    assert (got.open, got.close) == ("clean", "clean")


def test_a_stale_reconciliation_is_an_unavailable_episode() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    # minutes 200..209 (17:50:00Z to 17:59:30Z) keep seeing the 17:49:40Z document
    frozen = Z("2026-11-16T17:49:40Z")
    samples = replace_minutes(every_30s(NOV16), NOV16, range(200, 210), rec_as_of=frozen)
    got = acc.reconciliation_summary(cal, samples)
    # 17:51:30Z sees it 110 s old (fresh); 17:52:00Z 140 s old: unavailable from then
    assert got.episodes == (
        contract.ReconciliationEpisode(
            started_at=Z("2026-11-16T17:52:00Z"),
            status="unavailable",
            resolved_at=Z("2026-11-16T17:59:40Z"),
        ),
    )


def test_reconciliation_open_and_close_windows() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    # no document for minutes 0..2, then clean: a not-clean open covered by an episode
    samples = replace_minutes(every_30s(NOV16), NOV16, range(3), rec=None)
    got = acc.reconciliation_summary(cal, samples)
    assert got.open == "unavailable"
    assert got.episodes == (
        contract.ReconciliationEpisode(
            started_at=Z("2026-11-16T14:30:00Z"),
            status="unavailable",
            resolved_at=Z("2026-11-16T14:32:40Z"),
        ),
    )
    # required from minute 385 (20:55:00Z) to the close: unresolved, not clean at close
    late = replace_minutes(every_30s(NOV16), NOV16, range(385, 390), rec="reconciliation_required")
    got = acc.reconciliation_summary(cal, late)
    assert got.close == "reconciliation_required"
    assert got.episodes == (
        contract.ReconciliationEpisode(
            started_at=Z("2026-11-16T20:54:40Z"),
            status="reconciliation_required",
            resolved_at=None,
        ),
    )


def test_reconciliation_not_observed_in_a_window_is_unknown() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    # nothing sampled in the first 15 minutes (the open window ends 14:45:00Z)
    samples = [s for s in every_30s(NOV16) if s.at >= Z("2026-11-16T14:45:30Z")]
    got = acc.reconciliation_summary(cal, samples)
    assert got.open is None and got.close == "clean" and got.episodes == ()
    assert acc.reconciliation_summary(cal, []) == acc.ReconciliationSummary(None, None, None)


def test_exit_guard_nothing_exposed_is_zero_whatever_the_heartbeat() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    assert acc.max_exit_unguarded_s(cal, every_30s(NOV16, beat_age=999)) == 0
    assert acc.max_exit_unguarded_s(cal, every_30s(NOV16, exposed=True)) == 0
    assert acc.max_exit_unguarded_s(cal, []) is None


def test_exit_guard_stale_halt_unknown_and_unsampled_stretches() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    base = every_30s(NOV16, exposed=True)
    # minutes 100..105 stale (16:10:00Z to 16:15:30Z), fresh again 16:16:00Z: 360 s
    stale = replace_minutes(base, NOV16, range(100, 106), exposed=True, beat_age=300)
    assert acc.max_exit_unguarded_s(cal, stale) == 360
    # a standing HALT counts as unguarded: 16:10:00Z to 16:12:00Z
    halted = replace_minutes(base, NOV16, range(100, 102), exposed=True, halt=True)
    assert acc.max_exit_unguarded_s(cal, halted) == 120
    # an unreadable book (exposure unknown) for 16:10:00Z..16:11:00Z: 90 s
    unknown = [
        sample(s.at, beat_age=None)
        if Z("2026-11-16T16:10:00Z") <= s.at <= Z("2026-11-16T16:11:00Z")
        else s
        for s in base
    ]
    assert acc.max_exit_unguarded_s(cal, unknown) == 90
    # nothing sampled 16:10:00Z..16:19:30Z while exposed: 16:09:30Z to 16:20:00Z
    gap = [s for s in base if not Z("2026-11-16T16:10:00Z") <= s.at < Z("2026-11-16T16:20:00Z")]
    assert acc.max_exit_unguarded_s(cal, gap) == 630
    # unguarded at the last sample (20:59:30Z) runs to the close: 30 s, from 20:59:30Z
    tail = [*base[:-1], sample(Z("2026-11-16T20:59:30Z"), exposed=True, beat_age=500)]
    assert acc.max_exit_unguarded_s(cal, tail) == 30


def test_exit_guard_first_sample_late_while_exposed_counts_from_the_open() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    late = [s for s in every_30s(NOV16, exposed=True) if s.at >= Z("2026-11-16T14:35:00Z")]
    assert acc.max_exit_unguarded_s(cal, late) == 300
    flat = [s for s in every_30s(NOV16) if s.at >= Z("2026-11-16T14:35:00Z")]
    assert acc.max_exit_unguarded_s(cal, flat) == 0


def outside_fill(n: int, at: str, perm: int, **changes: Any):
    return ledger_event(
        event_id=f"fill:0000aaaa.0000{n:04d}.01.01",
        exec_id=f"0000aaaa.0000{n:04d}.01.01",
        base_exec_id=f"0000aaaa.0000{n:04d}.01",
        at=at,
        recorded_at=at,
        perm_id=perm,
        order_ref="",
        attribution={"intent_id": None, "strategy_version": None, "program_id": None},
        **changes,
    )


def test_unowned_orders_since_the_previous_close() -> None:
    prev_close, close = Z("2026-11-13T21:00:00Z"), Z("2026-11-16T21:00:00Z")
    ours = ledger_event(at="2026-11-16T15:00:00Z", recorded_at="2026-11-16T15:00:01Z")
    events = [
        ours,
        outside_fill(1, "2026-11-16T15:00:00Z", 111),
        outside_fill(2, "2026-11-16T15:00:05Z", 111),  # a second fill of the same order
        outside_fill(3, "2026-11-13T20:00:00Z", 222),  # before the previous close
        outside_fill(4, "2026-11-14T15:00:00Z", 333),  # a Saturday: since the previous close
        outside_fill(5, "2026-11-16T21:00:01Z", 444),  # after this close
    ]
    assert acc.unowned_orders(events, prev_close, close) == 2
    assert acc.unowned_orders([ours], prev_close, close) == 0


def test_session_record_is_the_contract_shape_with_reasons_for_unknowns() -> None:
    cal = acc.calendar_session(CAL, NOV16)
    ev = (contract.EvidenceRef(path="samples/2026-11-16.jsonl", sha256="b" * 64),)
    record = acc.build_session_record(
        cal,
        every_30s(NOV16),
        ledger=[outside_fill(1, "2026-11-16T15:00:00Z", 111)],
        ledger_reason=None,
        previous_close=Z("2026-11-13T21:00:00Z"),
        evidence=ev,
    )
    assert record == contract.SessionRecord(
        session=NOV16,
        heartbeat_minutes=390,
        reconciliation_open="clean",
        reconciliation_close="clean",
        reconciliation_episodes=(),
        unowned_orders=1,
        max_exit_unguarded_s=0,
        unavailable={},
        evidence=ev,
    )
    unknown = acc.build_session_record(
        cal,
        [],
        ledger=None,
        ledger_reason="ledger_unreadable",
        previous_close=Z("2026-11-13T21:00:00Z"),
        evidence=ev,
    )
    assert unknown.heartbeat_minutes == 0  # measured: no fresh minute was observed
    assert unknown.unavailable == {
        "reconciliation_open": "reconciliation_not_observed",
        "reconciliation_close": "reconciliation_not_observed",
        "reconciliation_episodes": "reconciliation_not_observed",
        "unowned_orders": "ledger_unreadable",
        "max_exit_unguarded_s": "samples_missing",
    }


# =============================================================================
# lifecycles (2026-10-22, EDT: 13:30Z to 20:00Z; the fixture's equity entry)
# =============================================================================

INTENT = "eq-spy-001"
CON = 756733


def entry_fill():
    return ledger_event()  # +2 at 14:03:05Z, trex:sup:eq-spy-001


def exit_fill(**changes: Any):
    values: dict[str, Any] = {
        "event_id": "fill:0000e0d5.6576d6a6.01.01",
        "exec_id": "0000e0d5.6576d6a6.01.01",
        "base_exec_id": "0000e0d5.6576d6a6.01",
        "at": "2026-10-22T15:10:00Z",
        "recorded_at": "2026-10-22T15:10:01Z",
        "quantity_delta": -2,
        "cash_delta_micros": 1_350_000_000,
        "order_ref": f"trex:desk:{INTENT}",
        "perm_id": 1234567891,
    }
    values.update(changes)
    return ledger_event(**values)


def commission(fill, at: str = "2026-10-22T15:20:00Z"):
    return ledger_event(
        event_id=f"commission:{fill.exec_id}",
        kind="commission",
        exec_id=fill.exec_id,
        base_exec_id=fill.base_exec_id,
        at=at,
        recorded_at=at,
        quantity_delta=0,
        cash_delta_micros=-1_000_000,
        order_ref=fill.order_ref,
    )


def closed(reason: str, structure: str = INTENT, at: str = "2026-10-22T15:10:05Z"):
    return acc.DeskExit(structure=structure, event="closed", reason=reason, at=Z(at))


def point(at: str, status: str = "clean", mismatches: frozenset[int] | None = frozenset()):
    return acc.RecPoint(at=Z(at), status=status, mismatch_con_ids=mismatches)


def lifecycle(ledger, exits=(), points=(), now: str = "2026-10-22T21:00:00Z", **kw: Any):
    return acc.build_lifecycle(
        mandate_v2(),
        asset_kind=kw.pop("asset_kind", "equity"),
        ledger=ledger,
        desk_exits=list(exits),
        points=list(points),
        now=Z(now),
        evidence=(),
    )


FULL = [entry_fill(), commission(entry_fill()), exit_fill(), commission(exit_fill())]


def test_completed_policy_lifecycle() -> None:
    record = lifecycle(
        FULL,
        [closed("stop")],
        [point("2026-10-22T15:09:00Z"), point("2026-10-22T15:10:30Z")],
    )
    assert record == contract.LifecycleRecord(
        intent_id=INTENT,
        asset_kind="equity",
        proposal_id="03480209a044f5707b002402160c279d036c4f05c678ad15ab9e13eb76ee2d48",
        approval_key="6f1c2b8e-3d4a-4f5b-9c6d-7e8f9a0b1c2d",
        approved_at=Z("2026-10-22T14:02:00Z"),
        filled_quantity=2,
        first_fill_at=Z("2026-10-22T14:03:05Z"),
        exit_by="policy",
        exited_at=Z("2026-10-22T15:10:00Z"),
        broker_flat_at=Z("2026-10-22T15:10:30Z"),
        reconciled_at=Z("2026-10-22T15:10:30Z"),
        commissions_complete=True,
        commissions_unavailable_reason=None,
        evidence=(),
    )


@pytest.mark.parametrize(
    ("exits", "expected"),
    [
        ([closed("flatten")], "flatten"),
        ([closed("target")], "policy"),
        ([closed("time_stop")], "policy"),
        ([closed("operator_confirmed_flat")], "manual"),
        ([closed("flat")], "manual"),
        ([acc.DeskExit(INTENT, "exit_begin", "forced", Z("2026-10-22T15:09:50Z"))], "policy"),
        ([], "manual"),  # a desk-tagged close nobody explains is never a policy exit
        ([closed("flatten", structure="another")], "manual"),
    ],
)
def test_exit_by_comes_from_the_desk_close_reason(exits, expected) -> None:
    assert lifecycle(FULL, exits).exit_by == expected


def test_not_exited_and_not_filled() -> None:
    held = lifecycle([entry_fill(), commission(entry_fill())], [], [point("2026-10-22T16:00:00Z")])
    assert (held.exit_by, held.exited_at, held.broker_flat_at, held.reconciled_at) == (
        "not_exited",
        None,
        None,
        None,
    )
    assert (held.filled_quantity, held.first_fill_at) == (2, Z("2026-10-22T14:03:05Z"))
    nothing = lifecycle([], [])
    assert (nothing.filled_quantity, nothing.first_fill_at, nothing.exit_by) == (
        0,
        None,
        "not_exited",
    )
    assert nothing.commissions_complete is True  # no execution, nothing to charge


def test_broker_flat_and_reconciled_follow_the_booked_exit() -> None:
    # a document from before the exit was booked (15:10:01Z) proves nothing
    early = lifecycle(FULL, [closed("stop")], [point("2026-10-22T15:10:00Z")])
    assert (early.broker_flat_at, early.reconciled_at) == (None, None)
    # required on this contract, then clean
    pts = [
        point("2026-10-22T15:10:30Z", "reconciliation_required", frozenset({CON})),
        point("2026-10-22T15:11:30Z"),
    ]
    later = lifecycle(FULL, [closed("stop")], pts)
    assert later.broker_flat_at == later.reconciled_at == Z("2026-10-22T15:11:30Z")
    # required on ANOTHER contract: our contract is flat at 15:10:30Z, clean at 15:11:30Z
    pts[0] = point("2026-10-22T15:10:30Z", "reconciliation_required", frozenset({999}))
    other = lifecycle(FULL, [closed("stop")], pts)
    assert other.broker_flat_at == Z("2026-10-22T15:10:30Z")
    assert other.reconciled_at == Z("2026-10-22T15:11:30Z")
    # a ledger marker (no mismatch detail) that is not clean proves nothing
    marker = lifecycle(
        FULL, [closed("stop")], [point("2026-10-22T15:10:30Z", "reconciliation_required", None)]
    )
    assert marker.broker_flat_at is None


def test_commissions_pending_on_the_day_missing_after_it() -> None:
    ledger = [entry_fill(), commission(entry_fill()), exit_fill()]
    same_day = lifecycle(ledger, [closed("stop")], now="2026-10-22T23:00:00Z")
    assert (same_day.commissions_complete, same_day.commissions_unavailable_reason) == (
        None,
        "commission_report_pending",
    )
    next_day = lifecycle(ledger, [closed("stop")], now="2026-10-23T13:00:00Z")
    assert (next_day.commissions_complete, next_day.commissions_unavailable_reason) == (False, None)


def test_option_lifecycle_two_legs_closed_by_its_structure() -> None:
    def leg(n: int, con: int, qty: int, ref: str, at: str):
        return ledger_event(
            event_id=f"fill:0000bbbb.0000{n:04d}.01.01",
            exec_id=f"0000bbbb.0000{n:04d}.01.01",
            base_exec_id=f"0000bbbb.0000{n:04d}.01",
            con_id=con,
            sec_type="OPT",
            quantity_delta=qty,
            order_ref=ref,
            at=at,
            recorded_at=at,
            perm_id=500 + n,
        )

    sup, desk = f"trex:sup:{INTENT}", "trex:desk:spread-7"
    fills = [
        leg(1, 11, 1, sup, "2026-10-22T14:03:00Z"),
        leg(2, 12, -1, sup, "2026-10-22T14:03:00Z"),
        leg(3, 11, -1, desk, "2026-10-22T16:00:00Z"),
        leg(4, 12, 1, desk, "2026-10-22T16:00:02Z"),
    ]
    ledger = fills + [commission(f) for f in fills]
    record = lifecycle(
        ledger,
        [closed("time_stop", structure="spread-7")],
        [point("2026-10-22T16:01:00Z")],
        asset_kind="option",
    )
    assert (record.filled_quantity, record.exit_by) == (1, "policy")
    assert record.exited_at == Z("2026-10-22T16:00:02Z")  # the fill that left both legs flat
    assert record.reconciled_at == Z("2026-10-22T16:01:00Z")


def test_desk_exits_are_read_from_the_desk_event_log(tmp_path) -> None:
    log = tmp_path / "events.jsonl"
    lines = [
        {
            "ts": "2026-10-22T11:10:05-04:00",
            "event": "closed",
            "structure": INTENT,
            "reason": "stop",
        },
        {
            "ts": "2026-10-22T11:09:50-04:00",
            "event": "exit_begin",
            "structure": INTENT,
            "reason": "stop",
        },
        {"ts": "2026-10-22T11:00:00-04:00", "event": "registered", "structure": INTENT},
    ]
    log.write_text("\n".join(json.dumps(x) for x in lines) + '\n{"event": "closed", broken\n')
    exits, invalid = acc.read_desk_exits(log)
    assert invalid == 1
    assert exits == [
        acc.DeskExit(INTENT, "closed", "stop", Z("2026-10-22T15:10:05Z")),
        acc.DeskExit(INTENT, "exit_begin", "stop", Z("2026-10-22T15:09:50Z")),
    ]
    assert acc.read_desk_exits(tmp_path / "absent.jsonl") == ([], 0)


# =============================================================================
# publish: evidence in, a verified trex-acceptance/1 out
# =============================================================================


def write_session_samples(adir: AcceptanceDir, samples) -> None:
    """Bulk-write sample files in the recorder's line format (one canonical
    document per line, by ET date; ``append_sample`` is pinned above)."""
    lines: dict[Path, list[bytes]] = {}
    for s in samples:
        path = acc.sample_path(adir, contract.et_date(s.at))
        lines.setdefault(path, []).append(model_bytes(s) + b"\n")
    for path, chunk in lines.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as fh:
            fh.write(b"".join(chunk))


@pytest.fixture
def three_sessions(tmp_path, sources):
    adir = AcceptanceDir(tmp_path / "acceptance")
    write_session_samples(adir, every_30s(NOV16))
    stale = replace_minutes(every_30s(NOV17), NOV17, range(100, 105), beat_age=200)
    write_session_samples(adir, stale)  # 385 of 390: below 99%
    write_session_samples(adir, every_30s(NOV18))
    acc.append_sample(adir, sample(Z("2026-11-21T15:00:00Z")))  # a Saturday file is ignored
    return adir


def publish(adir, sources, now: str, owners=frozenset({OWNER}), **kw: Any):
    return acc.publish(
        adir, sources, owners=owners, calendar=CAL, now=Z(now), take_sample=False, **kw
    )


def test_publish_builds_a_verified_document_from_the_evidence(three_sessions, sources) -> None:
    adir = three_sessions
    out = publish(adir, sources, "2026-11-18T21:10:00Z")
    assert out.problems == ()
    doc = contract.AcceptanceDocument.model_validate_json(adir.document().read_bytes())
    contract.verify_acceptance(doc, CAL)
    assert doc == out.document
    assert [(r.session, r.qualifying, r.reasons) for r in doc.session_results] == [
        (NOV16, True, ()),
        (NOV17, False, ("heartbeat_below_99pct",)),
        (NOV18, True, ()),
    ]
    assert (doc.streak.current_sessions, doc.streak.best_sessions) == (1, 1)
    assert doc.verdict == "not_met" and doc.live_authorized is False
    assert doc.owners == (OWNER,)
    assert [d.status for d in doc.drills] == ["not_run"] * 7
    # each persisted record cites an immutable copy of its in-session samples
    for day in (NOV16, NOV17, NOV18):
        record = contract.SessionRecord.model_validate_json(adir.session(day).read_bytes())
        assert record in doc.sessions
        (ref,) = record.evidence
        assert ref.path == f"sessions/{day.isoformat()}/samples.jsonl"
        cited = adir.evidence() / ref.path
        assert hashlib.sha256(cited.read_bytes()).hexdigest() == ref.sha256
        assert len(cited.read_bytes().splitlines()) == 780


def test_session_records_are_rebuildable_from_the_evidence(three_sessions, sources) -> None:
    adir = three_sessions
    publish(adir, sources, "2026-11-18T21:10:00Z")
    first = adir.document().read_bytes()
    records = {d: adir.session(d).read_bytes() for d in (NOV16, NOV17, NOV18)}
    for day in records:
        adir.session(day).unlink()
    publish(adir, sources, "2026-11-18T21:10:00Z")
    assert adir.document().read_bytes() == first
    assert {d: adir.session(d).read_bytes() for d in records} == records
    # a tampered record is replaced only by an explicit rebuild
    tampered = json.loads(records[NOV17])
    tampered["heartbeat_minutes"] = 390
    adir.session(NOV17).write_text(json.dumps(tampered))
    assert publish(adir, sources, "2026-11-18T21:10:00Z").document.streak.current_sessions == 3
    rebuilt = publish(adir, sources, "2026-11-18T21:10:00Z", rebuild=True)
    assert rebuilt.document.streak.current_sessions == 1
    assert adir.session(NOV17).read_bytes() == records[NOV17]


def test_a_record_naming_another_session_is_rebuilt(three_sessions, sources) -> None:
    adir = three_sessions
    publish(adir, sources, "2026-11-18T21:10:00Z")
    original = adir.session(NOV17).read_bytes()
    adir.session(NOV17).write_bytes(adir.session(NOV16).read_bytes())  # a qualifying copy
    out = publish(adir, sources, "2026-11-18T21:10:00Z")
    assert out.problems == ("session_record_invalid:2026-11-17",)
    assert out.document.streak.current_sessions == 1
    assert adir.session(NOV17).read_bytes() == original


def test_a_session_inside_its_settle_time_counts_but_is_not_persisted(
    three_sessions, sources
) -> None:
    adir = three_sessions
    out = publish(adir, sources, "2026-11-18T21:04:59Z")  # close 21:00Z + 300 s settle
    assert out.document is not None
    assert [r.session for r in out.document.session_results] == [NOV16, NOV17, NOV18]
    assert out.document.session_results[-1].qualifying is True
    assert not adir.session(NOV18).exists() and adir.session(NOV17).exists()
    (ref,) = next(s for s in out.document.sessions if s.session == NOV18).evidence
    assert ref.path == "samples/2026-11-18.jsonl"  # provisional: the live sample file


def test_a_session_before_its_close_is_not_in_the_window(three_sessions, sources) -> None:
    out = publish(three_sessions, sources, "2026-11-18T20:59:59Z")
    assert [r.session for r in out.document.session_results] == [NOV16, NOV17]


def test_no_owner_no_document(three_sessions, sources) -> None:
    out = publish(three_sessions, sources, "2026-11-18T21:10:00Z", owners=frozenset())
    assert out.document is None and "owners_not_configured" in out.problems
    assert not three_sessions.document().exists()


def test_a_missing_recorded_session_resets_the_streak(tmp_path, sources) -> None:
    adir = AcceptanceDir(tmp_path / "acceptance")
    write_session_samples(adir, every_30s(NOV16))
    write_session_samples(adir, every_30s(NOV18))  # 2026-11-17 never recorded
    doc = publish(adir, sources, "2026-11-18T21:10:00Z").document
    assert [(r.session, r.recorded) for r in doc.session_results] == [
        (NOV16, True),
        (NOV17, False),
        (NOV18, True),
    ]
    assert doc.streak.current_sessions == 1


def seed_lifecycle(sources: acc.Sources) -> None:
    proposal = entry_proposal(equity_effect())
    (sources.run_dir / "proposals").mkdir()
    (sources.run_dir / "proposals" / f"{proposal.proposal_id}.json").write_bytes(
        proposal_bytes(proposal)
    )
    mandate = mandate_v2(proposal)
    (sources.supervised_dir / "mandates" / f"{INTENT}.consumed.json").write_bytes(
        model_bytes(mandate)
    )
    (sources.supervised_dir / "mandates" / "eq-unsent.json").write_bytes(model_bytes(mandate))
    ledger = [*FULL]
    ledger.append(
        ledger_event(
            event_id="reconciliation:" + "c" * 64,
            kind="reconciliation",
            exec_id=None,
            base_exec_id=None,
            con_id=None,
            sec_type=None,
            perm_id=None,
            order_ref=None,
            quantity_delta=0,
            cash_delta_micros=0,
            ref_sha256="c" * 64,
            detail="clean",
            at="2026-10-22T15:10:30Z",
            recorded_at="2026-10-22T15:10:30Z",
            attribution={"intent_id": None, "strategy_version": None, "program_id": None},
        )
    )
    (sources.run_dir / "ledger" / "events.jsonl").write_bytes(
        b"".join(model_bytes(e) + b"\n" for e in ledger)
    )
    exits = {
        "ts": "2026-10-22T11:10:05-04:00",
        "event": "closed",
        "structure": INTENT,
        "reason": "stop",
    }
    (sources.run_dir / "events.jsonl").write_text(json.dumps(exits) + "\n")


def test_publish_follows_consumed_mandates_into_lifecycles(tmp_path, sources) -> None:
    adir = AcceptanceDir(tmp_path / "acceptance")
    seed_lifecycle(sources)
    oct22 = date(2026, 10, 22)
    # no reconciliation document 15:00:00Z..15:19:30Z: the ledger marker
    # (15:10:30Z) is the first clean reconciliation after the booked exit
    samples = replace_minutes(every_30s(oct22), oct22, range(90, 110), rec=None)
    write_session_samples(adir, samples)
    out = publish(adir, sources, "2026-10-23T13:00:00Z")
    assert out.problems == ()
    (life,) = out.document.lifecycles  # the unconsumed mandate sent nothing
    assert (life.intent_id, life.asset_kind, life.exit_by) == (INTENT, "equity", "policy")
    assert life.reconciled_at == Z("2026-10-22T15:10:30Z")  # the ledger marker
    (result,) = out.document.lifecycle_results
    assert result.completed is True and result.counted is True
    stored = contract.LifecycleRecord.model_validate_json(adir.lifecycle(INTENT).read_bytes())
    assert stored == life
    (ref,) = life.evidence
    assert ref.path == f"lifecycles/{INTENT}/facts.json"
    facts = adir.evidence() / ref.path
    assert hashlib.sha256(facts.read_bytes()).hexdigest() == ref.sha256
    assert json.loads(facts.read_bytes())["mandate"]["intent_id"] == INTENT


def _ok_drill_check(adir: AcceptanceDir, drill_id: str) -> contract.EvidenceRef:
    """An ok ``trex-drill-check/1`` under the evidence root (as the drills CLI writes)."""
    from tree_options.trex.drills import DrillCheck, Requirement

    check = DrillCheck(
        drill_id=drill_id,
        since=Z("2026-11-16T21:00:00Z"),
        until=Z("2026-11-16T21:30:00Z"),
        checked_at=Z("2026-11-16T21:31:00Z"),
        requirements=(Requirement(name="relogged_in", met=True, detail="seen"),),
        findings=(),
        artifacts=(),
        ok=True,
        live_authorized=False,
    )
    data = model_bytes(check)
    path = f"drills/{drill_id}/20261116T213100Z-check.json"
    (adir.evidence() / path).parent.mkdir(parents=True, exist_ok=True)
    (adir.evidence() / path).write_bytes(data)
    return contract.EvidenceRef(path=path, sha256=hashlib.sha256(data).hexdigest())


def test_drill_records_reach_the_document(tmp_path, sources) -> None:
    adir = AcceptanceDir(tmp_path / "acceptance")
    write_session_samples(adir, every_30s(NOV16))
    drill = contract.DrillRecord(
        id="relogin",
        status="passed",
        evidence=(_ok_drill_check(adir, "relogin"),),
        attested_by=OWNER,
        at=Z("2026-11-16T22:00:00Z"),
        note="",
    )
    adir.drill("relogin").parent.mkdir(parents=True)
    adir.drill("relogin").write_bytes(model_bytes(drill))
    adir.drill("gateway_outage").write_text("{broken")
    out = publish(adir, sources, "2026-11-16T22:10:00Z")
    assert out.document.drills[0] == drill
    assert out.document.drill_results[0].passed is True
    assert out.document.drills[2].status == "not_run"
    assert out.problems == ("drill_record_invalid:gateway_outage",)


@pytest.mark.parametrize(
    "evidence",
    ["missing", "wrong_hash", "other_drill", "not_ok"],
)
def test_a_stored_passed_drill_without_its_ok_check_never_counts(
    tmp_path, sources, evidence: str
) -> None:
    """Integration fix (review P2): the publisher applies the drills CLI's
    rule at compute time, so a hand-written "passed" record with no ok
    machine check of the same drill is reported and counted as not run."""
    adir = AcceptanceDir(tmp_path / "acceptance")
    write_session_samples(adir, every_30s(NOV16))
    if evidence == "missing":
        ref = contract.EvidenceRef(path="drills/never/written.json", sha256="0" * 64)
    elif evidence == "wrong_hash":
        ref = _ok_drill_check(adir, "relogin").model_copy(update={"sha256": "0" * 64})
    elif evidence == "other_drill":
        ref = _ok_drill_check(adir, "backup_restore")
    else:
        ref = _ok_drill_check(adir, "relogin")
        doc = json.loads((adir.evidence() / ref.path).read_bytes())
        doc["ok"] = False
        doc["requirements"][0]["met"] = False
        data = json.dumps(doc).encode()
        (adir.evidence() / ref.path).write_bytes(data)
        ref = contract.EvidenceRef(path=ref.path, sha256=hashlib.sha256(data).hexdigest())
    forged = contract.DrillRecord(
        id="relogin",
        status="passed",
        evidence=(ref,),
        attested_by=OWNER,
        at=Z("2026-11-16T22:00:00Z"),
        note="",
    )
    adir.drill("relogin").parent.mkdir(parents=True, exist_ok=True)
    adir.drill("relogin").write_bytes(model_bytes(forged))
    out = publish(adir, sources, "2026-11-16T22:10:00Z")
    assert out.problems == ("drill_evidence_invalid:relogin",)
    assert out.document.drills[0].status == "not_run"
    assert out.document.drill_results[0].passed is False
    assert out.document.drills_passed == 0


def test_publish_takes_a_sample_inside_the_window(tmp_path, sources) -> None:
    adir = AcceptanceDir(tmp_path / "acceptance")
    acc.publish(
        adir, sources, owners=frozenset({OWNER}), calendar=CAL, now=Z("2026-11-16T15:00:00Z")
    )
    (got,) = acc.read_samples(adir.evidence() / "samples/2026-11-16.jsonl").samples
    assert (
        got.at == Z("2026-11-16T15:00:00Z") and got.heartbeat_unavailable_reason == "book_missing"
    )
    acc.publish(
        adir, sources, owners=frozenset({OWNER}), calendar=CAL, now=Z("2026-11-16T23:00:00Z")
    )
    assert len(acc.read_samples(adir.evidence() / "samples/2026-11-16.jsonl").samples) == 1


def test_the_document_round_trips_through_the_contract(three_sessions, sources) -> None:
    doc = publish(three_sessions, sources, "2026-11-18T21:10:00Z").document
    assert three_sessions.document().read_bytes() == model_bytes(doc)
    assert model_document(doc)["live_authorized"] is False


# =============================================================================
# the CLI
# =============================================================================


def run_main(argv, tmp_path, sources, env, now: str, capsys):
    base = [
        "--acceptance-dir",
        str(tmp_path / "acceptance"),
        "--run-dir",
        str(sources.run_dir),
        "--supervised-dir",
        str(sources.supervised_dir),
        "--gateway-state",
        str(sources.gateway_state),
    ]
    code = acc.main([*argv, *base], env=env, now=Z(now))
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_cli_publish_ok_degraded_and_bad_config(tmp_path, sources, capsys) -> None:
    adir = AcceptanceDir(tmp_path / "acceptance")
    write_session_samples(adir, every_30s(NOV16))
    code, out = run_main(
        ["publish", "--no-sample"],
        tmp_path,
        sources,
        {"TREX_OWNER_EMAILS": OWNER},
        "2026-11-16T22:10:00Z",
        capsys,
    )
    assert code == 0
    assert out["status"] == "ok" and out["verdict"] == "not_met"
    assert out["streak_sessions"] == 1 and out["live_authorized"] is False
    code, out = run_main(
        ["publish", "--no-sample"], tmp_path, sources, {}, "2026-11-16T22:10:00Z", capsys
    )
    assert code == 3 and out["problems"] == ["owners_not_configured"]
    code, out = run_main(
        ["publish"],
        tmp_path,
        sources,
        {"TREX_OWNER_EMAILS": "not an email"},
        "2026-11-16T22:10:00Z",
        capsys,
    )
    assert code == 2 and out["status"] == "refused"


# =============================================================================
# hygiene
# =============================================================================

NEW_PYTHON = [
    REPO / "src/tree_options/trex/acceptance.py",
    REPO / "src/tree_options/trex/drills.py",
    REPO / "src/tree_options/trex_web/acceptance_view.py",
]


def test_lane_modules_load_no_broker_library() -> None:
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    probe = (
        "import sys, tree_options.trex.acceptance, tree_options.trex.drills;"
        " sys.exit(1 if 'ib_async' in sys.modules or 'fastapi' in sys.modules else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe], cwd=REPO, env=env, capture_output=True, timeout=120
    )
    assert done.returncode == 0, done.stderr.decode()[-2000:]


@pytest.mark.parametrize("path", NEW_PYTHON, ids=lambda p: p.name)
def test_no_en_or_em_dashes_and_no_timedelta(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for dash in (chr(0x2013), chr(0x2014)):
        assert dash not in text, f"{path.name} contains U+{ord(dash):04X}"
    assert "timedelta" not in text
    assert ".weekday(" not in text and "toordinal" not in text


def test_utc_helper_is_aware() -> None:
    assert Z("2026-11-16T15:00:00Z").tzinfo == UTC
