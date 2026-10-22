"""A supervised paper desk driven by commands, over the shared Wave 2 fakes.

Everything real except the gateway (``Wave2Gateway``), the clock
(``SessionClock``), the command authority (``FakeCommandAuthority``) and
the account snapshot's timestamp (stamped from the session clock, as
``test_supervised_desk``'s ``ClockedIbkr``): the web's own ``CommandStore``
queues every command exactly as the web backend does (accepted, never
applied), and the desk's ``run_loop`` consumes it.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.unit.trex_fakes import (
    CLOCK_START,
    SPY_CON_ID,
    FakeCommandAuthority,
    SessionClock,
    Wave2Gateway,
    equity_effect,
)
from tree_options.action_graph.canary import EQUITY_ENTRY_OPEN_DELAY_S
from tree_options.desk.enter_supervised import DRAFT_SCHEMA, LIVE_BINDINGS, draft_directory
from tree_options.time.sessions import shift_instant
from tree_options.trex.account import AccountSnapshot
from tree_options.trex.contracts import (
    CommandResult,
    SupervisedEffect,
    SupervisedEquityEffect,
    effect_v1_sha256,
    model_document,
    parse_command,
)
from tree_options.trex.contracts.proposals import PROPOSAL_SCHEMA
from tree_options.trex.desk_entries import equity_draft
from tree_options.trex.desk_runtime import DeskPaths, DeskRuntime
from tree_options.trex.ibkr import IbkrTrex
from tree_options.trex.plan import LegStructure
from tree_options.trex.supervised import SupervisedPaths
from tree_options.trex.supervised_desk import SupervisedDesk, run_loop
from tree_options.trex.supervised_ibkr import (
    SUPERVISED_CLIENT_ID,
    IbkrSupervisedBroker,
    supervised_order_ref,
)
from tree_options.trex_web.command_store import CommandStore

ACCOUNT = "DU0000001"
EQUITY_PROGRAM = "prog-equity-pilot"
EQUITY_STRATEGY = "equity-pilot/1"
OPTION_PROGRAM = "desk-miner"
OPTION_STRATEGY = "operational-canary/1"
OPTION_EXPIRY = "20261120"
QQQ_CON_ID = 320227571
#: the option legs' conIds (an underlying other than SPY: the canary's
#: flat-book rule reads the canary's own underlying only)
OPTION_CON_IDS = {
    ("OPT", "QQQ", OPTION_EXPIRY, 500.0, "P"): 9500,
    ("OPT", "QQQ", OPTION_EXPIRY, 495.0, "P"): 9495,
}
PROFILE: dict[str, Any] = {
    "profile_id": "owner-paper",
    "revision": 1,
    "intended_capital": "5000",
    "risk_style": "defined-risk",
    "goals": ["paper-desk"],
    "allowed_strategy_versions": [EQUITY_STRATEGY, OPTION_STRATEGY],
    # the fixture equity entry reserves $268.60 (its 20% gap reserve)
    "max_loss_per_trade": "300",
    "max_open_loss": "400",
    "max_daily_loss": "600",
    "horizon_days": 30,
}
#: the shared fakes' clock opens AT the session open (09:30 ET), five
#: minutes before the equity entry window (lane E2: open + 5 min to close -
#: 30 min); the rig starts five minutes INSIDE the window, so every scenario
#: may enter unless it moves the clock out on purpose
RIG_START = shift_instant(CLOCK_START, EQUITY_ENTRY_OPEN_DELAY_S + 5 * 60)


class ClockedIbkr(IbkrTrex):
    """The account snapshot stamped by the session clock (the real one reads
    the wall clock, which the fixed test clock would call stale)."""

    clock: SessionClock

    def account_snapshot(self) -> AccountSnapshot | None:
        return AccountSnapshot(
            account_id=ACCOUNT,
            net_liquidation=Decimal("1000000"),
            cash=Decimal("1000000"),
            buying_power=Decimal("4000000"),
            currency="USD",
            ts=self.clock(),
        )


def _iso(instant: datetime) -> str:
    return instant.isoformat()


class OwnerRig:
    """One desk run dir, one supervised dir, one gateway, one clock."""

    def __init__(
        self, tmp: Path, *, profile: dict[str, Any] | None = None, start: datetime | None = None
    ) -> None:
        self.clock = SessionClock(RIG_START if start is None else start)
        self.gw = Wave2Gateway(con_ids=dict(OPTION_CON_IDS), clock=self.clock)
        self.gw.add_stock("QQQ", QQQ_CON_ID)  # the option underlying's spot stream
        self.ib = ClockedIbkr(client_id=SUPERVISED_CLIENT_ID)
        self.ib.clock = self.clock
        self.ib._ib = self.gw
        self.paths = DeskPaths(tmp / "desk")
        self.sup = SupervisedPaths(tmp / "supervised")
        self.sup.prepare()
        self.paths.root.mkdir(parents=True, exist_ok=True)
        (self.paths.root / "profile.json").write_text(json.dumps(profile or PROFILE))
        self.auth = FakeCommandAuthority(clock=self.clock)
        self.store = CommandStore(self.paths.root)
        self.notified: list[tuple[str, str, str]] = []
        self.started: dict[str, Any] = {}
        self.desk = self._desk()

    def _desk(self) -> SupervisedDesk:
        runtime = DeskRuntime(self.ib, self.paths, supervised=self.sup, clock=self.clock)
        runtime.acquire()
        runtime.notify = lambda title, message, priority="default": self.notified.append(
            (title, message, priority)
        )
        desk = SupervisedDesk(
            self.ib,
            runtime,
            IbkrSupervisedBroker(self.ib, clock=self.clock),
            supervised=self.sup,
            owner_epoch=self.clock.owner_epoch,
            clock=self.clock,
        )
        desk.write_owner()
        self.started = desk.start()
        return desk

    def restart(self, *, down_s: float = 60) -> None:
        """A desk stop/start: a fresh process (new owner epoch), state on disk."""
        self.desk.runtime.release()
        self.clock.restart(down_s=down_s)
        self.desk = self._desk()

    def close(self) -> None:
        self.desk.runtime.release()

    # -- the loop --------------------------------------------------------------

    def step(self) -> int:
        """One owner loop iteration (no sleep: the market moves on gw.sleep)."""
        return run_loop(self.desk, interval_s=0, stop=lambda: False, max_ticks=1)

    def wait(self, seconds: float = 1) -> None:
        """The market moves one step (fills, ticks, cancels, late reports)."""
        self.gw.sleep(seconds)

    def quote(self, bid: int = 67148, ask: int = 67152, symbol: str = "SPY") -> None:
        """A fresh real-time NBBO a second later (queued until subscribed)."""
        self.clock.advance(1)
        try:
            self.gw.push_tick(symbol, bid, ask)
        except AssertionError:
            self.gw.script_ticks(symbol, [(bid, ask)])

    def option_quotes(self) -> None:
        """Two-sided leg quotes ticking now (a 1.00-wide debit at ~0.90)."""
        self.gw.quote(9500, 2.00, 2.10)
        self.gw.quote(9495, 1.10, 1.20)
        for con in (9500, 9495):
            self.gw.tickers[con].time = self.clock.utc()  # type: ignore[attr-defined]

    # -- candidates -------------------------------------------------------------

    def draft_equity(
        self, effect: SupervisedEquityEffect | None = None, *, valid_s: int = 3 * 3600
    ) -> SupervisedEquityEffect:
        effect = effect or equity_effect()
        doc = equity_draft(
            effect,
            program_id=EQUITY_PROGRAM,
            strategy_version=EQUITY_STRATEGY,
            valid_until=shift_instant(self.clock.utc(), valid_s),
        )
        self._write_draft(doc)
        return effect

    def draft_option(self, intent_id: str = "opt-qqq-001", *, valid_s: int = 3 * 3600) -> Any:
        structure = LegStructure.model_validate(
            {
                "id": intent_id,
                "underlying": "QQQ",
                "kind": "debit_vertical",
                "legs": [
                    {"right": "P", "action": "BUY", "strike": "500", "expiry": "2026-11-20"},
                    {"right": "P", "action": "SELL", "strike": "495", "expiry": "2026-11-20"},
                ],
                "quantity": 1,
                "entry_date": self.clock.today(),
                "exit_deadline": self.clock.next_session(),
                "limit": "1.00",
                "exits": {"touch": False, "breach": False},
            }
        )
        effect = SupervisedEffect(
            intent_id=intent_id,
            account_id="DUT143714",  # the drafts' annotation account; the desk rebinds
            structure=structure,
            side=structure.open_side,
            quantity=1,
            limit=Decimal("0.90"),
            order_ref=supervised_order_ref(intent_id),
        )
        cost = 9000  # 0.90 x 100 x 1, in cents
        doc = {
            "schema": DRAFT_SCHEMA,
            "draft_of": PROPOSAL_SCHEMA,
            "candidate_id": intent_id,
            "kind": "debit_vertical",
            "valid_until": _iso(shift_instant(self.clock.utc(), valid_s)),
            "execution_enabled": False,
            "missing": list(LIVE_BINDINGS),
            "body": {
                "schema": PROPOSAL_SCHEMA,
                "effect": model_document(effect),
                "effect_sha256": effect_v1_sha256(effect),
                "strategy_version": OPTION_STRATEGY,
                "program_id": OPTION_PROGRAM,
                "sleeve_ref": None,
                "proposed_at": None,
                "send_deadline": None,
                "review": {
                    "quote": None,
                    "max_loss_cents": cost,
                    "notional_cents": cost,
                    "screening_sha256": None,
                    "owner_epoch": None,
                    "profile_digest": None,
                },
            },
        }
        self._write_draft(doc)
        return effect

    def _write_draft(self, doc: dict[str, Any]) -> None:
        folder = draft_directory(self.paths.root)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{doc['candidate_id']}.json").write_text(json.dumps(doc))

    # -- commands ---------------------------------------------------------------

    def submit(
        self,
        kind: str,
        args: dict[str, Any],
        *,
        sub: str = "owner",
        ttl_s: int = 120,
        key: str | None = None,
    ) -> str:
        """Queue one command through the web's store (accepted, never applied)."""
        now = self.clock.utc()
        doc = {
            "schema": "trex-command/1",
            "idempotency_key": key or str(uuid.uuid4()),
            "kind": kind,
            "args": args,
            "issued_at": _iso(now),
            "expires_at": _iso(shift_instant(now, ttl_s)),
        }
        body = json.dumps(doc).encode()
        actor = self.auth.verify(self.auth.make_request(body, family_sub=sub))
        command = parse_command({**doc, "issued_by": model_document(actor)})
        submission = self.store.submit(command, now=now)
        assert submission.result.status == "accepted", submission.result
        return command.idempotency_key

    def result(self, key: str) -> CommandResult:
        result = self.store.read_result(key)
        assert result is not None
        return result

    def run(self, kind: str, args: dict[str, Any], **kwargs: Any) -> CommandResult:
        """Queue one command, run one loop iteration, return the desk's answer."""
        key = self.submit(kind, args, **kwargs)
        assert self.step() == 0
        return self.result(key)

    def compose(self, candidate_id: str = "eq-spy-001", **args: Any) -> CommandResult:
        kind = args.pop("asset_kind", "equity")
        program = args.pop("program_id", EQUITY_PROGRAM if kind == "equity" else OPTION_PROGRAM)
        return self.run(
            "compose_entry",
            {"program_id": program, "candidate_id": candidate_id, "asset_kind": kind, **args},
        )

    @staticmethod
    def approval_args(composed: CommandResult) -> dict[str, Any]:
        facts = composed.result or {}
        return {
            name: facts[name]
            for name in (
                "proposal_id",
                "proposal_sha256",
                "effect_sha256",
                "owner_epoch",
                "profile_digest",
            )
        }

    def approve(self, composed: CommandResult, **changes: Any) -> CommandResult:
        return self.run("approve_entry", {**self.approval_args(composed), **changes})

    # -- reads -------------------------------------------------------------------

    def events(self) -> list[dict[str, Any]]:
        path = self.paths.events()
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def names(self) -> list[str]:
        return [str(e["event"]) for e in self.events()]

    def book(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.paths.book().read_text())["structures"]
        return loaded

    def orders(self, action: str | None = None) -> list[Any]:
        return [t for t in self.gw.trades if action is None or t.order.action == action]

    def held(self) -> float:
        return sum(float(p.position) for p in self.gw.positions() if p.contract.conId == SPY_CON_ID)

    def ledger_file(self, name: str) -> Path:
        return self.paths.root / "ledger" / name
