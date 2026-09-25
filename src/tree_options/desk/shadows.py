"""Forward shadow tracking of every surfaced desk deal (plan D7).

The miner's queue is advisory until the operator rules the selection, but
the evidence clock cannot wait for the ruling: every deal the miner
surfaces, whatever its status, is tracked here as a shadow episode so the
shadow week and the promotion scorecards have history the day admission
turns on.

An episode is at most one deal per ``(underlying, row, ISO week)`` of the
entry session (the miner often surfaces hundreds of candidates of one row
on one name; one representative carries the family's forward evidence).
The representative is the queue's own ranking: admissible first, then the
lowest rank, then deal id.

Marking uses the recorded chain store only, never live quotes. Each later
recorded session between the entry and the exit deadline adds a mark with
two prices:

* ``mid``: the package mid under the miner's own sign convention, present
  only when every leg has a two-sided quote with ``0 < bid <= ask`` (a
  0.00/0.00 leg book is NO market, fix ``1cf94bf``);
* ``realistic``: the package value closing at the adverse side, BUY legs
  at their bid and SELL legs at their ask. A long leg pinned at a zero
  bid closes at zero: that is a price, not missing data.

Resolution prices the exit at the deadline session's chain, or, when that
session is missing or does not price the package, at the latest recorded
session at or before the deadline that does, flagged ``fallback`` (the
2026-09-23 vendor gap). P&L is dollars per package: debit kinds earn
``exit_value - fill``, credit kinds ``fill - exit_value``, times 100
shares times quantity, all in the entry orientation.

Money fields are strings in every written document, the house convention.
Scorecards (:mod:`tree_options.desk.scorecards`) consume the episode
documents; nothing here computes promotion rules.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tree_options.desk import paths, surface
from tree_options.desk.miner import ADMISSIBLE
from tree_options.desk.sessions import (
    Calendar,
    cutoff_instant,
    latest_completed_session,
)
from tree_options.desk.store import atomic_write_json, read_chain
from tree_options.trex.plan import CREDIT_KINDS

SCHEMA = "desk-shadow/1"
STAGE = "shadows"
SHARES_PER_CONTRACT = Decimal(100)
_ZERO = Decimal(0)


# ------------------------------------------------------------------- weeks


def week_key(d: date) -> str:
    """The ISO week of a session, ``YYYY-Www`` (episodes dedupe per week)."""
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


# ------------------------------------------------------------- leg pricing


@dataclass(frozen=True)
class LegRef:
    """One leg of a surfaced deal, as the queue document carries it."""

    right: str
    action: str
    strike: Decimal
    expiry: date


def leg_refs(deal: Mapping[str, Any]) -> list[LegRef]:
    out: list[LegRef] = []
    for leg in deal["legs"]:
        out.append(
            LegRef(
                right=leg["right"],
                action=leg["action"],
                strike=Decimal(str(leg["strike"])),
                expiry=date.fromisoformat(str(leg["expiry"])),
            )
        )
    return out


def _dec(x: Any) -> Decimal | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return Decimal(repr(f))


def package_prices(
    kind: str, legs: Sequence[LegRef], doc: Mapping[str, Any]
) -> tuple[Decimal | None, Decimal] | None:
    """``(mid, realistic)`` of the package from one recorded chain, or None
    when any leg is absent from the chain or its book is not a book
    (``bid``/``ask`` missing, negative, or crossed).

    ``mid`` is None unless every leg has ``0 < bid <= ask`` (a one-sided or
    zero book is no mid). ``realistic`` needs only the adverse side (bid
    for BUY legs, ask for SELL legs) with ``0 <= bid <= ask``: a long leg
    pinned at a zero bid closes at zero. Signs follow the miner's
    ``entry_terms`` convention, flipped on credit kinds.
    """
    credit = kind in CREDIT_KINDS
    cols = doc["columns"]
    have: dict[tuple[str, str, str], tuple[Decimal | None, Decimal | None]] = {}
    for i, exp in enumerate(cols["exp"]):
        key = (str(cols["right"][i]), repr(float(cols["strike"][i])), str(exp))
        have.setdefault(key, (_dec(cols["bid"][i]), _dec(cols["ask"][i])))
    mid: Decimal | None = _ZERO
    realistic = _ZERO
    for g in legs:
        got = have.get((g.right, repr(float(g.strike)), g.expiry.isoformat()))
        if got is None:
            return None
        bid, ask = got
        if bid is None or ask is None or not _ZERO <= bid <= ask:
            return None
        sign = (1 if g.action == "BUY" else -1) * (-1 if credit else 1)
        realistic += sign * (bid if g.action == "BUY" else ask)
        if _ZERO < bid <= ask:
            if mid is not None:
                mid += sign * ((bid + ask) / 2)
        else:
            mid = None  # keep walking: realistic may still be complete
    return mid, realistic


def pnl_dollars(kind: str, entry_fill: Decimal, exit_value: Decimal, quantity: int) -> Decimal:
    """Realized P&L in dollars closing a package worth ``exit_value``
    (entry orientation) that was filled at ``entry_fill``."""
    per = (entry_fill - exit_value) if kind in CREDIT_KINDS else (exit_value - entry_fill)
    return per * SHARES_PER_CONTRACT * quantity


# ---------------------------------------------------------------- episodes


@dataclass
class Mark:
    session: date
    mid: Decimal | None
    realistic: Decimal
    spot: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session.isoformat(),
            "mid": str(self.mid) if self.mid is not None else None,
            "realistic": str(self.realistic),
            "spot": self.spot,
        }


@dataclass
class Episode:
    deal_id: str
    row: str
    row_title: str
    tier: str
    name: str
    kind: str
    quantity: int
    legs: list[LegRef]
    entry_session: date
    exit_deadline: date | None
    fill: Decimal | None
    width: Decimal | None
    max_loss: Decimal | None
    decision: Mapping[str, Any] | None
    mine_status: str
    reasons: list[str]
    marks: list[Mark] = field(default_factory=list)
    resolved: Mapping[str, Any] | None = None

    @property
    def state(self) -> str:
        return "resolved" if self.resolved is not None else "open"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "deal_id": self.deal_id,
            "row": self.row,
            "row_title": self.row_title,
            "tier": self.tier,
            "underlying": self.name,
            "kind": self.kind,
            "quantity": self.quantity,
            "legs": [
                {
                    "right": g.right,
                    "action": g.action,
                    "strike": str(g.strike),
                    "expiry": g.expiry.isoformat(),
                }
                for g in self.legs
            ],
            "entry_session": self.entry_session.isoformat(),
            "exit_deadline": self.exit_deadline.isoformat() if self.exit_deadline else None,
            "fill": str(self.fill) if self.fill is not None else None,
            "width": str(self.width) if self.width is not None else None,
            "max_loss": str(self.max_loss) if self.max_loss is not None else None,
            "decision": dict(self.decision) if self.decision else None,
            "mine_status": self.mine_status,
            "mine_reasons": list(self.reasons),
            "marks": [m.as_dict() for m in self.marks],
            "resolution": dict(self.resolved) if self.resolved else None,
            "state": self.state,
        }


def episode_from_deal(deal: Mapping[str, Any]) -> Episode:
    """The tracking record of one surfaced/admissible queue deal."""
    deadline = deal.get("exit_deadline")

    def opt(key: str) -> Decimal | None:
        v = deal.get(key)
        return _dec(v) if v is not None else None

    return Episode(
        deal_id=str(deal["deal_id"]),
        row=str(deal["row"]),
        row_title=str(deal.get("row_title") or ""),
        tier=str(deal.get("tier") or ""),
        name=str(deal["underlying"]),
        kind=str(deal["kind"]),
        quantity=int(deal.get("quantity") or 1),
        legs=leg_refs(deal),
        entry_session=date.fromisoformat(str(deal["entry_session"])),
        exit_deadline=date.fromisoformat(deadline) if deadline else None,
        fill=opt("fill"),
        width=opt("width"),
        max_loss=opt("max_loss"),
        decision=dict(deal["decision"]) if deal.get("decision") else None,
        mine_status=str(deal.get("status") or ""),
        reasons=[str(r) for r in deal.get("reasons") or []],
    )


def episode_from_doc(doc: Mapping[str, Any]) -> Episode:
    """Rebuild an episode from its written document."""
    ep = episode_from_deal(
        {
            "deal_id": doc["deal_id"],
            "row": doc["row"],
            "row_title": doc.get("row_title"),
            "tier": doc.get("tier"),
            "underlying": doc["underlying"],
            "kind": doc["kind"],
            "quantity": doc["quantity"],
            "legs": doc["legs"],
            "entry_session": doc["entry_session"],
            "exit_deadline": doc.get("exit_deadline"),
            "fill": doc.get("fill"),
            "width": doc.get("width"),
            "max_loss": doc.get("max_loss"),
            "decision": doc.get("decision"),
            "status": doc.get("mine_status"),
            "reasons": doc.get("mine_reasons"),
        }
    )
    ep.marks = [
        Mark(
            session=date.fromisoformat(str(m["session"])),
            mid=Decimal(str(m["mid"])) if m.get("mid") is not None else None,
            realistic=Decimal(str(m["realistic"])),
            spot=m.get("spot"),
        )
        for m in doc.get("marks") or []
    ]
    ep.resolved = dict(doc["resolution"]) if doc.get("resolution") else None
    return ep


# -------------------------------------------------------------- selection


def select_episodes(
    queue: Mapping[str, Any], claimed: Mapping[str, str]
) -> tuple[list[Episode], dict[str, str]]:
    """One episode per ``(underlying, row, week)`` not already claimed.

    Preference order: admissible deals first, then the queue's rank, then
    deal id. ``claimed`` maps ``name|row|week`` to the deal id already
    tracking it and is returned extended with this queue's claims.
    """
    out: list[Episode] = []
    taken: set[str] = set()
    cand = sorted(
        list(queue.get("admissible") or []) + list(queue.get("surfaced") or []),
        key=lambda d: (
            0 if d.get("status") == ADMISSIBLE else 1,
            d.get("rank") if isinstance(d.get("rank"), int) else 10**9,
            str(d.get("deal_id")),
        ),
    )
    for deal in cand:
        ep = episode_from_deal(deal)
        key = f"{ep.name}|{ep.row}|{week_key(ep.entry_session)}"
        if key in claimed or key in taken:
            continue
        taken.add(key)
        out.append(ep)
    claims = dict(claimed)
    for ep in out:
        claims[f"{ep.name}|{ep.row}|{week_key(ep.entry_session)}"] = ep.deal_id
    return out, claims


# ------------------------------------------------------------ update pass


@dataclass
class ShadowResult:
    exit_code: int
    status: str
    detail: str
    session: date | None
    new_episodes: int = 0
    marked: int = 0
    resolved: int = 0

    def line(self) -> str:
        return (
            f"shadows session={self.session} status={self.status} "
            f"new={self.new_episodes} marked={self.marked} resolved={self.resolved}"
        ) + (f" ({self.detail})" if self.detail else "")


def _recorded_sessions(store_root: Path) -> list[date]:
    chains = store_root / "chains"
    if not chains.is_dir():
        return []
    return sorted(date.fromisoformat(p.name) for p in chains.iterdir() if p.is_dir())


def _chain_doc(store_root: Path, session: date, sym: str) -> dict[str, Any] | None:
    path = store_root / "chains" / session.isoformat() / f"{sym}.json.gz"
    if not path.exists():
        return None
    try:
        return read_chain(path)
    except (OSError, ValueError):
        return None


def _episode_path(state: Path, deal_id: str) -> Path:
    return state / "episodes" / f"{deal_id}.json"


def _mark_episode(ep: Episode, store_root: Path, sessions: Sequence[date]) -> int:
    """Append marks for every recorded session after the last mark through
    the deadline. Returns the number of marks added."""
    if ep.exit_deadline is None or ep.resolved is not None:
        return 0
    last = ep.marks[-1].session if ep.marks else ep.entry_session
    added = 0
    for s in sessions:
        if s <= last or s > ep.exit_deadline:
            continue
        doc = _chain_doc(store_root, s, ep.name)
        if doc is None:
            continue
        prices = package_prices(ep.kind, ep.legs, doc)
        if prices is None:
            continue
        mid, realistic = prices
        ep.marks.append(Mark(s, mid, realistic, surface.chain_spot(doc)))
        added += 1
    return added


def _resolve(ep: Episode, store_root: Path, sessions: Sequence[date]) -> bool:
    """Resolve at the deadline session's chain, else at the latest recorded
    session at or before the deadline that prices the package (the
    vendor-gap fallback). Idempotent."""
    if ep.resolved is not None or ep.exit_deadline is None or ep.fill is None:
        return False
    deadline = ep.exit_deadline
    # Wait until the deadline window is over: a recorded session at or past
    # the deadline proves no later mark can arrive for this episode.
    if not any(s >= deadline for s in sessions):
        return False
    usable = [s for s in sessions if ep.entry_session < s <= deadline]
    chosen: tuple[date, Decimal | None, Decimal] | None = None
    # Reversed: the deadline session itself wins when its chain prices the
    # package, else the latest recorded session before it that does.
    for s in reversed(usable):
        doc = _chain_doc(store_root, s, ep.name)
        if doc is None:
            continue
        prices = package_prices(ep.kind, ep.legs, doc)
        if prices is None:
            continue
        chosen = (s, prices[0], prices[1])
        break
    if chosen is None:
        return False
    session, mid, realistic = chosen
    fallback = session != deadline
    pnl = pnl_dollars(ep.kind, ep.fill, realistic, ep.quantity)
    first = next((m for m in ep.marks if m.session > ep.entry_session), None)
    ev = ep.decision.get("ev") if ep.decision else None
    ep.resolved = {
        "session": session.isoformat(),
        "deadline": deadline.isoformat(),
        "fallback": fallback,
        "mid": str(mid) if mid is not None else None,
        "realistic": str(realistic),
        "pnl_dollars": str(pnl),
        "decision_ev": str(ev) if ev is not None else None,
        "pnl_minus_ev_dollars": str(pnl - Decimal(str(ev))) if ev is not None else None,
        "first_mark_realistic": str(first.realistic) if first else None,
        "slippage_first_mark_dollars": (
            str(pnl_dollars(ep.kind, ep.fill, first.realistic, ep.quantity)) if first else None
        ),
    }
    return True


def load_episodes(state: Path) -> list[Episode]:
    eps: list[Episode] = []
    d = state / "episodes"
    if not d.is_dir():
        return eps
    for p in sorted(d.glob("*.json")):
        try:
            eps.append(episode_from_doc(json.loads(p.read_text())))
        except (OSError, ValueError, KeyError):
            continue  # an unreadable episode never blocks the pass
    return eps


def update_shadows(
    *,
    session: date | None,
    now: datetime,
    cal: Calendar,
    state: Path | None = None,
    store_root: Path | None = None,
    queue_dir: Path | None = None,
    dry_run: bool = False,
) -> ShadowResult:
    """The shadows pass of one session: adopt the queue's representatives,
    mark open episodes forward, resolve at deadlines.

    Default session: the latest completed session at ``now``. Exit 3 (no
    marker written, retry at the next slot) when the session's queue file
    is missing; a present-but-empty queue is a normal, successful pass
    (nothing surfaced that day). Re-running never duplicates: episodes are
    keyed by deal id and weeks are claimed in ``status.json``.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    d = latest_completed_session(now, cal) if session is None else session
    if d is None or not cal.is_session(d):
        return ShadowResult(2, "bad_session", f"{d} is not an NYSE session", d)
    if session is not None and now < cutoff_instant(d):
        return ShadowResult(2, "not_closed", f"{d} has not closed yet (16:15 ET)", d)
    state = (state or paths.state_root() / "shadows").resolve()
    store_root = (store_root or paths.store_root()).resolve()
    queue_dir = (queue_dir or paths.queue_dir()).resolve()
    queue_path = queue_dir / f"{d.isoformat()}.json"
    if not queue_path.exists():
        return ShadowResult(3, "not_ready", f"no queue for {d}", d)
    queue = json.loads(queue_path.read_text())
    status_path = state / "status.json"
    claimed: dict[str, str] = {}
    if status_path.exists():
        try:
            claimed = {
                str(k): str(v)
                for k, v in (json.loads(status_path.read_text()).get("claimed") or {}).items()
            }
        except (OSError, ValueError):
            claimed = {}
    sessions = _recorded_sessions(store_root)
    by_id = {e.deal_id: e for e in load_episodes(state)}
    new_eps, claimed = select_episodes(queue, claimed)
    for ep in new_eps:
        by_id.setdefault(ep.deal_id, ep)
    marked = resolved = 0
    for ep in by_id.values():
        if ep.resolved is None:
            marked += _mark_episode(ep, store_root, sessions)
            if _resolve(ep, store_root, sessions):
                resolved += 1
    if not dry_run:
        (state / "episodes").mkdir(parents=True, exist_ok=True)
        for ep in by_id.values():
            atomic_write_json(_episode_path(state, ep.deal_id), ep.as_dict())
        atomic_write_json(
            status_path,
            {"schema": SCHEMA, "claimed": claimed, "updated_at": now.isoformat()},
        )
        marker = paths.state_root() / "stages" / d.isoformat() / f"{STAGE}.done.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            marker,
            {
                "session": d.isoformat(),
                "at": now.isoformat(),
                "new_episodes": len(new_eps),
                "marked": marked,
                "resolved": resolved,
            },
        )
    return ShadowResult(
        0, "ok", "", d, new_episodes=len(new_eps), marked=marked, resolved=resolved
    )
