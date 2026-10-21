"""The live entry sender honors PAUSE_ENTRIES and HALT.

``SupervisedDesk`` is the one process that sends entries. A desk run dir
with ``PAUSE_ENTRIES`` (the owner's entries-only pause) or ``HALT`` must
answer a queued request ``blocked`` before it spends any authority: no
mandate use, no recorded intent, no permit, no registered spec, no broker
order. A pause or HALT placed while the request is being screened (the
screening reads the broker) still stops the send. Exits are never gated by
the pause: the runtime places a due exit while entries are paused.

Oracles: the fake gateway's placed orders, the supervised outbox and permit
files, the desk book and the published result files. The rig is the
supervised desk suite's own (``tests.unit.test_supervised_desk.Rig``).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_supervised_desk import ACCOUNT, Rig, _effect
from tests.unit.trex_fakes import position_row
from tree_options.trex import admission, pause
from tree_options.trex.clock import ET
from tree_options.trex.contracts.vocabulary import Blocker

PAUSED = Blocker.ENTRIES_PAUSED.value
HALTED = "halt"
#: the rig structure's exit deadline (``_vertical``: 2026-10-09): a time stop
EXIT_DAY = datetime(2026, 10, 9, 10, 0, tzinfo=ET)


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    r = Rig(tmp_path)
    yield r
    r.runtime.release()


def _pause(rig: Rig) -> None:
    pause.pause_entries(
        rig.paths.root, actor="operator-terminal", reason="gate test", now=rig.clock.now
    )


def _halt(rig: Rig) -> None:
    rig.paths.halt().touch()


STOPS = {"paused": (_pause, PAUSED), "halted": (_halt, HALTED)}


def _nothing_spent(rig: Rig, intent_id: str = "sup-001") -> None:
    """No order, no outbox entry, no permit, no spec for the intent."""
    assert rig.gw.trades == []
    for path in (
        rig.sup.pending(intent_id),
        rig.sup.sending(intent_id),
        rig.sup.terminal(intent_id),
    ):
        assert not path.exists(), path.name
    permits = rig.sup.root / "permits"
    assert not permits.is_dir() or sorted(p.name for p in permits.iterdir()) == []
    assert rig.runtime.specs() == {}


@pytest.mark.parametrize("stop", sorted(STOPS))
def test_a_stopped_desk_answers_a_queued_request_blocked_and_sends_nothing(rig, stop):
    place, blocker = STOPS[stop]
    rig.ready()
    rig.request()  # queued before the stop, well inside its send deadline
    place(rig)
    assert blocker in admission.entry_blockers(rig.paths.root, rig.clock.now, account_id=ACCOUNT)
    (result,) = rig.desk.process_requests()
    assert (result["status"], result.get("blockers")) == ("blocked", [blocker]), result
    assert "screening_sha256" not in result, "refused before the screening read the broker"
    assert rig.result()["blockers"] == [blocker], "the answer is published"
    _nothing_spent(rig)


def test_the_blocked_request_spent_no_authority_and_resume_admits_a_new_one(rig):
    """The mandate (one order) is untouched by the refused request, the
    refused request is never sent later, and resume_entries re-opens entries."""
    rig.ready()
    _pause(rig)
    rig.request()
    (refused,) = rig.desk.process_requests()
    assert refused.get("blockers") == [PAUSED], refused
    pause.resume_entries(rig.paths.root, actor="operator-terminal", now=rig.clock.now)
    assert rig.desk.process_requests() == [], "a refused request stays answered"
    rig.request(_effect(intent_id="sup-002"), name="sup-002")
    (sent,) = rig.desk.process_requests()
    assert sent["status"] == "sent", sent
    (trade,) = rig.gw.trades
    assert trade.order.orderRef == "trex:sup:sup-002"


@pytest.mark.parametrize("stop", sorted(STOPS))
def test_a_stop_placed_during_screening_still_stops_the_send(rig, monkeypatch, stop):
    place, blocker = STOPS[stop]
    rig.ready()
    rig.request()
    real = rig.desk.broker.preflight

    def preflight(payload: bytes) -> list[str]:
        place(rig)  # the owner pauses (or halts) while the desk reads the broker
        return real(payload)

    monkeypatch.setattr(rig.desk.broker, "preflight", preflight)
    (result,) = rig.desk.process_requests()
    assert (result["status"], result.get("blockers")) == ("blocked", [blocker]), result
    assert result["screening_sha256"], "refused after the screening, before any authority"
    _nothing_spent(rig)


def _open_position(rig: Rig) -> Any:
    rig.ready()
    rig.request()
    (sent,) = rig.desk.process_requests()
    assert sent["status"] == "sent", sent
    rig.runtime.tick()
    (entry,) = rig.gw.trades
    entry.orderStatus.status = "Filled"
    entry.orderStatus.filled = 1
    entry.orderStatus.avgFillPrice = 0.90
    rig.gw.position_rows[:] = [
        position_row(100, 1, account=ACCOUNT, symbol="SPY"),
        position_row(95, -1, account=ACCOUNT, symbol="SPY"),
    ]
    rig.runtime.tick()
    assert _book(rig)["status"] == "open"
    return entry


def _book(rig: Rig) -> dict[str, Any]:
    return json.loads(rig.paths.book().read_text())["structures"]["dv1"]


def test_exits_still_run_while_entries_are_paused(rig):
    _open_position(rig)
    _pause(rig)
    rig.request(_effect(intent_id="sup-002"), name="sup-002")
    (refused,) = rig.desk.process_requests()
    assert (refused["status"], refused.get("blockers")) == ("blocked", [PAUSED])
    assert len(rig.gw.trades) == 1, "the paused desk placed no entry"
    rig.clock.now = EXIT_DAY
    rig.tick_quotes()
    rig.runtime.tick()
    assert pause.is_paused(rig.paths.root)
    book = _book(rig)
    assert (book["status"], book["exit_reason"]) == ("exit_working", "time_stop")
    exit_trade = rig.gw.trades[-1]
    assert (exit_trade.order.orderRef, exit_trade.order.action) == ("trex:desk:dv1", "SELL")
