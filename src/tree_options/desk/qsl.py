"""QSL: the quote-bearing shadow ledger's deterministic candidate queue.

SPEC 3 of the 2026-09-30 strategy set (``~/.local/state/trex-strategy-20260930/
strategy-specs.md``): pre-register a fixed deterministic candidate set and feed
it through the ALREADY-LIVE shadow-evidence lane (:mod:`tree_options.desk.shadows`)
so the desk accumulates modeled shadow P&L at recorded two-sided snapshots.
These are simulated fills, not external executions. No model, no LLM, no orders; the queue is
a pure function of the recorded chain documents.

Per recorded session D (one chain snapshot per name, ~17:46 ET):

* for each name in ``chains/<D>/`` and each family (``put_credit``,
  ``call_debit`` — the two bullish survivors' shapes; both verticals have the
  LONG leg at the LOWER strike, ``intraday_action_graph.py:234-236``):
  ``short`` = the strike whose own BS delta at mid-implied IV
  (:func:`desk.surface.chain_quotes`, never the vendor column) is nearest
  |0.30| in the nearest expiry with 7 <= DTE <= 35 (calendar days from D);
  ``long`` = the listed strike below it at the width nearest 5;
* the SPEC 2 crossing-cost gate, all clauses, on BOTH legs from the same
  snapshot: two-sided (0 < bid <= ask), sized (bid_size and ask_size >= 10),
  open interest >= 100 (rails ``min_leg_open_interest``, v2.toml:47), and the
  buyer's crossing penalty <= 0.10 of the package mid (theta = rails'
  ``max_leg_spread_frac_of_mid``, v2.toml:48; the corrected buyer's-crossing
  arithmetic, never the seller's);
* a gate pass is enqueued as ONE ``trex.deal/1`` admissible row whose ``fill``
  IS the crossing price (``credit_x``/``debit_x``), never a modeled mid fill,
  with ``exit_deadline`` = the last session the lane's expiry safety buffer
  permits (see :func:`deadline_for`); the shadow lane then records EOD marks
  through that deadline from LATER snapshots, censored when absent.

Snapshot honesty (carried on every artifact): the record time is after the
close (~17:46 ET), where spreads are wider than the liquid session — the
ledger is conservative on crossing costs and measures snapshot-crossing
economics, not a 10:00-ET fill simulation.

Two points where SPEC 3's prose could not be taken literally, both forced by
the lane's own contracts and both frozen in the pre-registration
(``docs/desk/QSL-PREREGISTRATION.md``):

1. ``deadline = expiry`` is not expressible: ``desk.contracts.parse_deal``
   refuses ``deadline >= previous_session(first_expiry)``
   (``deadline_safety_buffer``) and :class:`tree_options.trex.plan.LegStructure`
   refuses ``exit_deadline >= first_expiry``. The deadline is therefore the
   session two before the first expiry (the engine's own expiry-safety bound,
   ``miner.exit_deadline``'s ``sessions[before - 2]``) and the resolution mark
   is that session's EOD crossing, not the expiry settlement.
2. ``long = short -/+ w`` reads as "minus for puts, plus for calls"; the plus
   sign would make the call family a call CREDIT (selling the nearer-the-money
   call), which is neither the named survivor ``call_debit`` nor the board
   grammar's shape. Both families take ``long = short - w``.

This producer is deliberately SEPARATE from the deal miner's lane: it writes
``<state>/queue-qsl/<D>.json`` (never ``<state>/queue`` — the miner owns those
bytes, and a shared evidence store would raise ``content_conflict`` on the
shared ``queue`` key), and the shadow runs that consume it pass
``--queue-dir <queue-qsl> --database <evidence/qsl.sqlite3>`` so the live
``desk-evidence.timer`` (default paths) is untouched.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from tree_options.desk import indices, paths, selection, surface
from tree_options.desk import playbook as desk_playbook
from tree_options.desk.miner import SCHEMA, deal_id, encode
from tree_options.desk.sessions import (
    Calendar,
    cutoff_instant,
    first_session_after,
    latest_completed_session,
    previous_session,
)
from tree_options.desk.store import atomic_create_bytes, atomic_write_bytes, atomic_write_json
from tree_options.trex.clock import ET
from tree_options.trex.plan import ExitRules, Leg, LegStructure, Right, cents

STAGE = "qsl"
CODE = "tree_options.desk.qsl"
#: fixed before any scoring (SPEC 3); the pre-registration this queue serves.
PREREG_ID = "QSL-20260930"
PREREG_DOC = "docs/desk/QSL-PREREGISTRATION.md"
#: sha256 of PREREG_DOC's committed bytes; tests/unit+desk_safety pin it.
PREREG_SHA256 = "390606d99a3169b085d77e58ed5c02d7529da24c36969ce01b60b62bdef6abc3"

QUANTITY = 1
TIER = "qsl"
ABS_DELTA_TARGET = Decimal("0.30")
DTE_MIN, DTE_MAX = 7, 35
WIDTH_TARGET = Decimal("5")
#: SPEC 2's crossing-penalty cap as a fraction of the package mid (= rails'
#: max_leg_spread_frac_of_mid, data/desk/playbook/v2.toml:48).
THETA = Decimal("0.10")
#: contracts quoted at the touch, both sides (SPEC 2's ``sized`` clause).
SIZE_FLOOR = 10
#: rails min_leg_open_interest (data/desk/playbook/v2.toml:47).
OI_FLOOR = 100
_CENT = Decimal("0.01")
_ZERO = Decimal(0)

FAMILIES: tuple[str, ...] = ("put_credit", "call_debit")
ROW_OF: dict[str, str] = {"put_credit": "qsl-put-credit", "call_debit": "qsl-call-debit"}
RIGHT_OF: dict[str, str] = {"put_credit": "P", "call_debit": "C"}
KIND_OF: dict[str, str] = {"put_credit": "credit_vertical", "call_debit": "debit_vertical"}
TITLE_OF: dict[str, str] = {
    "put_credit": "QSL put credit vertical (put_credit_expiry shape)",
    "call_debit": "QSL call debit vertical (call_debit_hold5 shape)",
}

RATIONALE = (
    "{row} ({tier}, prereg {prereg}): {kind} x{qty} on {name}, {legs}. "
    "Entry on {entry} at a crossing {orientation} of {fill} per package — the "
    "modeled {cross_name} from session {session}'s recorded chain "
    "({cross_detail}); package mid {mid}, crossing penalty {penalty} = "
    "{penalty_frac} of mid (gate <= {theta}). Max loss ${max_loss}. "
    "Deadline {exit}, the last session the lane's expiry-safety buffer permits "
    "before expiry {expiry}; the shadow lane records EOD marks through it, "
    "censored when absent. Snapshot-crossing economics at the post-close "
    "record time, a modeled shadow fill with no external execution evidence; no take-profit, stop, touch or "
    "breach exit is modeled."
)


def qsl_engine_manifest() -> dict[str, Any]:
    """Bind actual source bytes used for queue construction, plus shared helpers.

    This is source custody under the immutable deployed-code assumption; it
    does not claim resident bytecode or arbitrary injected-calendar identity.
    """
    from tree_options.data import massive_derived
    from tree_options.desk import ivhist, miner, pricing, sessions, store
    from tree_options.desk.longrun import longrun_engine_identity
    from tree_options.synth_options import greeks
    from tree_options.trex import clock, plan

    modules = {__name__: hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    for module in (
        indices,
        paths,
        selection,
        surface,
        desk_playbook,
        ivhist,
        miner,
        pricing,
        sessions,
        store,
        massive_derived,
        greeks,
        clock,
        plan,
    ):
        if module.__file__ is None:
            raise RuntimeError(f"QSL helper source unavailable: {module.__name__}")
        modules[module.__name__] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    manifest = {"modules": modules, "longrun_engine_sha256": longrun_engine_identity()}
    return {**manifest, "sha256": hashlib.sha256(encode(manifest)).hexdigest()}


def queue_dir() -> Path:
    """This lane's own queue dir (never the miner's)."""
    raw = os.environ.get("TREX_DESK_QSL_QUEUE", "").strip()
    return Path(raw) if raw else paths.state_root() / "queue-qsl"


def database_path() -> Path:
    """This lane's own evidence DB (never the live desk.sqlite3)."""
    raw = os.environ.get("TREX_DESK_QSL_DB", "").strip()
    return Path(raw) if raw else paths.state_root() / "evidence" / "qsl.sqlite3"


# --------------------------------------------------------------- the picker


@dataclass(frozen=True)
class LegQuote:
    """One leg's quote as this lane prices it (all from D's own snapshot)."""

    right: str
    strike: Decimal
    expiry: date
    dte: int
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal | None
    bid_size: int
    ask_size: int
    oi: int
    delta: float | None
    iv: float | None


def _dec(x: float | None) -> Decimal | None:
    return Decimal(repr(x)) if x is not None else None


def _sizes(doc: Mapping[str, Any]) -> dict[tuple[str, Decimal, str], tuple[int, int, int]]:
    """(right, strike, exp) -> (bid_size, ask_size, oi) from the raw columns.

    :func:`desk.surface.chain_quotes` carries oi but not the touch sizes; the
    SPEC 2 ``sized`` clause reads them here, straight off the recorded file.
    Anything not a plain int reads as -1 (below every floor: fail closed).
    """
    cols = doc["columns"]
    out: dict[tuple[str, Decimal, str], tuple[int, int, int]] = {}
    for i in range(len(cols["occ"])):
        key = (cols["right"][i], Decimal(str(cols["strike"][i])), cols["exp"][i])
        parts = []
        for col in ("bid_size", "ask_size", "oi"):
            v = cols[col][i]
            parts.append(v if type(v) is int else -1)
        out[key] = (parts[0], parts[1], parts[2])
    return out


def pick_expiry(quotes: list[surface.Quote]) -> date | None:
    """The nearest expiry with DTE_MIN <= DTE <= DTE_MAX (calendar days from D)."""
    ok = sorted({q.expiry for q in quotes if DTE_MIN <= q.dte <= DTE_MAX})
    return ok[0] if ok else None


def pick_short(quotes: list[surface.Quote], expiry: date, right: str) -> surface.Quote | None:
    """The strike whose own |delta| is nearest the target; ties -> lower strike."""
    same = [q for q in quotes if q.expiry == expiry and q.right == right and q.delta is not None]
    if not same:
        return None
    return min(
        same, key=lambda q: (abs(abs(cast(float, q.delta)) - float(ABS_DELTA_TARGET)), q.strike)
    )


def pick_long(
    quotes: list[surface.Quote], expiry: date, right: str, short_strike: float
) -> surface.Quote | None:
    """The listed strike BELOW the short at the width nearest WIDTH_TARGET.

    Ties -> the smaller width. The long is always the lower strike (both
    families are the bullish verticals of the board grammar).
    """
    cands = [
        q for q in quotes if q.expiry == expiry and q.right == right and q.strike < short_strike
    ]
    if not cands:
        return None
    return min(
        cands,
        key=lambda q: (
            abs(abs(Decimal(str(q.strike)) - Decimal(str(short_strike))) - WIDTH_TARGET),
            abs(Decimal(str(q.strike)) - Decimal(str(short_strike))),
        ),
    )


def deadline_for(first_expiry: date, entry: date, cal: Calendar) -> tuple[date | None, str]:
    """The exit deadline: the session two before the first expiry.

    ``desk.contracts.parse_deal`` refuses ``deadline >= previous_session(
    first_expiry)`` and the plan model refuses ``exit_deadline >= first_expiry``
    — "deadline = expiry" is not expressible in this lane, so the deadline is
    the engine's own expiry-safety bound (``miner.exit_deadline``'s
    ``sessions[before - 2]``) and the resolution mark is that session's EOD
    crossing. Frozen as such in the pre-registration.
    """
    sessions = cal.sessions()
    before = bisect.bisect_left(sessions, first_expiry)
    if before < 2:
        return None, f"no hold window: the calendar has no room before {first_expiry}"
    deadline = sessions[before - 2]
    if deadline <= entry:
        return None, f"no hold window: the deadline {deadline} is not after the entry {entry}"
    return deadline, ""


@dataclass(frozen=True)
class Gate:
    """SPEC 2's gate on one (name, family, session) candidate."""

    ok: bool
    reasons: tuple[str, ...]
    mid: Decimal | None  # the package mid: a credit for put_credit, a debit for call_debit
    cross: Decimal | None  # the executable entry at the buyer's crossing


def gate(family: str, short: LegQuote, long: LegQuote) -> Gate:
    """Every SPEC 2 clause on both legs, buyer's-crossing convention.

    ``credit_mid - credit_x`` (credit) and ``debit_x - debit_mid`` (debit) are
    the same quantity — the sum of the legs' half-spreads — so the one
    ``penalty`` test ``penalty <= theta * mid`` serves both families. Prices
    are quantized to cents the way exchanges round displayed mids
    (:func:`tree_options.trex.plan.cents`).
    """
    reasons: list[str] = []
    two_sided = True
    for leg, tag in ((short, "short"), (long, "long")):
        if leg.bid is None or leg.ask is None or not _ZERO < leg.bid <= leg.ask:
            two_sided = False
            reasons.append(f"no_two_sided_quote:{tag}")
    if not (
        short.bid_size >= SIZE_FLOOR
        and short.ask_size >= SIZE_FLOOR
        and long.bid_size >= SIZE_FLOOR
        and long.ask_size >= SIZE_FLOOR
    ):
        reasons.append("touch_size_below_floor")
    if not (short.oi >= OI_FLOOR and long.oi >= OI_FLOOR):
        reasons.append("open_interest_below_floor")
    mid = cross = None
    if (
        two_sided
        and short.mid is not None
        and long.mid is not None
        and short.bid is not None
        and short.ask is not None
        and long.bid is not None
        and long.ask is not None
    ):
        # the long is always the lower strike: for a put credit the short is
        # the nearer-the-money leg (sold at its bid, wing bought at its ask);
        # for a call debit the long is the nearer-the-money leg (bought at its
        # ask, the 0.30-delta wing sold at its bid).
        mid = cents(short.mid - long.mid) if family == "put_credit" else cents(long.mid - short.mid)
        cross = (
            cents(short.bid - long.ask) if family == "put_credit" else cents(long.ask - short.bid)
        )
    if mid is None or cross is None:
        reasons.append("no_package_price")
    else:
        penalty = (mid - cross) if family == "put_credit" else (cross - mid)
        if mid <= _ZERO:
            reasons.append("package_mid_not_positive")
        elif penalty > THETA * mid:
            reasons.append("crossing_penalty_above_theta")
    return Gate(not reasons, tuple(reasons), mid, cross)


def entry_terms(g: Gate, family: str, width: Decimal) -> tuple[Decimal, Decimal] | None:
    """The lane's own entry terms from a passing gate: the crossing ``fill``
    and the first cent strictly beyond it (the credit floor / debit cap,
    mirroring :func:`desk.miner.entry_terms`). None when no legal terms exist
    on that width (the miner's width refusals)."""
    assert g.cross is not None
    fill = g.cross
    limit = fill - _CENT if family == "put_credit" else fill + _CENT
    if not (_ZERO < limit < width and _ZERO < fill < width):
        return None
    return fill, limit


# --------------------------------------------------------------- the row


def _legs_text(legs: tuple[Leg, ...]) -> str:
    return ", ".join(f"{g.action} {g.right} {g.strike} {g.expiry.isoformat()}" for g in legs)


def _s(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


def _leg_quote(
    q: surface.Quote, sizes: Mapping[tuple[str, Decimal, str], tuple[int, int, int]]
) -> LegQuote:
    key = (q.right, Decimal(str(q.strike)), q.expiry.isoformat())
    bs, as_, oi = sizes.get(key, (-1, -1, -1))
    return LegQuote(
        q.right,
        Decimal(str(q.strike)),
        q.expiry,
        q.dte,
        _dec(q.bid),
        _dec(q.ask),
        _dec(q.mid),
        bs,
        as_,
        oi,
        q.delta,
        q.iv,
    )


def candidate_row(
    family: str,
    name: str,
    short: LegQuote,
    long: LegQuote,
    g: Gate,
    *,
    session: date,
    entry: date,
    deadline: date,
    rank: int,
    raw_sha256: str,
) -> dict[str, Any]:
    """One admissible ``trex.deal/1`` row. Internally consistent by
    construction: ``desk.contracts.parse_deal`` re-derives limit, width,
    max_loss and the leg identities and must accept it (a test proves the
    whole lane end to end)."""
    kind = KIND_OF[family]
    terms = entry_terms(g, family, abs(long.strike - short.strike))
    if terms is None:
        raise ValueError(f"{name} {family}: no legal entry terms on the chosen width")
    fill, limit = terms
    legs = (
        Leg(
            right=cast(Right, short.right), action="SELL", strike=short.strike, expiry=short.expiry
        ),
        Leg(right=cast(Right, long.right), action="BUY", strike=long.strike, expiry=long.expiry),
    )
    did = deal_id(session, ROW_OF[family], name, _key(kind, legs))
    struct = LegStructure(
        id=did,
        underlying=name,
        kind=cast(Any, kind),
        legs=legs,
        quantity=QUANTITY,
        entry_date=entry,
        exit_deadline=deadline,
        limit=limit,
        exits=ExitRules(touch=False, breach=False),
        deal_id=did,
        ref_mid=g.mid,
    )
    assert g.mid is not None
    mid = g.mid
    penalty = (mid - fill) if family == "put_credit" else (fill - mid)
    penalty_frac = (penalty / mid).quantize(Decimal("0.0001"))
    credit = family == "put_credit"
    return {
        "deal_id": did,
        "rank": rank,
        "status": "admissible",
        "reasons": [],
        "notes": [],
        "row": ROW_OF[family],
        "row_title": TITLE_OF[family],
        "tier": TIER,
        "underlying": name,
        "kind": kind,
        "quantity": QUANTITY,
        "legs": [
            {
                "right": g_.right,
                "action": g_.action,
                "strike": str(g_.strike),
                "expiry": g_.expiry.isoformat(),
                "ratio": 1,
                "bid": _s(leg.bid),
                "ask": _s(leg.ask),
                "oi": leg.oi,
                "iv": leg.iv,
                "delta": leg.delta,
            }
            for g_, leg in zip(struct.legs, (short, long), strict=True)
        ],
        "entry_session": entry.isoformat(),
        "exit_deadline": deadline.isoformat(),
        "width": str(struct.width),
        "ref_mid": str(mid),
        "fill": str(fill),
        "limit": str(limit),
        "max_loss": str(struct.max_loss()),
        "signal": None,
        "structure": struct.model_dump(mode="json"),
        "valuation": None,
        "decision": None,
        "rails": None,
        "qsl": {
            "evidence_kind": "SIMULATED EXECUTION",
            "execution_authorized": False,
            "exact_external_economics": False,
            "live_money": False,
            "family": family,
            "prereg_id": PREREG_ID,
            "prereg_sha256": PREREG_SHA256,
            "session": session.isoformat(),
            "chain_raw_sha256": raw_sha256,
            "expiry": short.expiry.isoformat(),
            "dte": short.dte,
            "abs_delta_short": round(short.delta, 6) if short.delta is not None else None,
            "package_mid": str(mid),
            "cross_entry": str(fill),
            "crossing_penalty": str(penalty),
            "crossing_penalty_frac": str(penalty_frac),
            "theta": str(THETA),
            "size_floor": SIZE_FLOOR,
            "oi_floor": OI_FLOOR,
            "bid_size": [short.bid_size, long.bid_size],
            "ask_size": [short.ask_size, long.ask_size],
            "oi": [short.oi, long.oi],
            "cost_convention": "buyer crossing both sides at the recorded snapshot",
        },
        "rationale": RATIONALE.format(
            row=ROW_OF[family],
            tier=TIER,
            prereg=PREREG_ID,
            kind=kind,
            qty=QUANTITY,
            name=name,
            legs=_legs_text(struct.legs),
            entry=entry.isoformat(),
            orientation="credit received" if credit else "debit paid",
            cross_name="credit_x" if credit else "debit_x",
            cross_detail=(
                f"short sold at bid {short.bid}, wing bought at ask {long.ask}"
                if credit
                else f"long bought at ask {long.ask}, wing sold at bid {short.bid}"
            ),
            fill=fill,
            mid=mid,
            penalty=penalty,
            penalty_frac=penalty_frac,
            theta=THETA,
            max_loss=struct.max_loss(),
            exit=deadline.isoformat(),
            expiry=short.expiry.isoformat(),
            session=session.isoformat(),
        ),
    }


def _key(kind: str, legs: tuple[Leg, ...]) -> str:
    parts = sorted(f"{g.expiry.isoformat()}:{g.right}:{g.action}:{g.strike}" for g in legs)
    return f"{kind}|" + ";".join(parts)


# --------------------------------------------------------------- the run


@dataclass(frozen=True)
class QslResult:
    exit_code: int  # 0 done | 3 inputs not ready (retry) | 1 failure or conflict | 2 bad args
    status: str
    detail: str = ""
    session: date | None = None
    payload: dict[str, Any] | None = None
    path: Path | None = None

    def line(self) -> str:
        d = self.session.isoformat() if self.session else "-"
        tail = f" {self.detail}" if self.detail else ""
        where = f" -> {self.path}" if self.path else ""
        return f"qsl {d} {self.status} rc={self.exit_code}{where}{tail}"

    def summary(self) -> list[str]:
        if self.payload is None:
            return []
        q = self.payload["qsl"]
        return [
            f"  names read={q['names_read']} gate_pass={q['gate_pass']} refused={q['refused']}",
            f"  families put_credit={q['family_pass']['put_credit']}"
            f" call_debit={q['family_pass']['call_debit']}",
        ]


def rate_for(session: date, store: Path, cal: Calendar) -> tuple[float | None, str]:
    """DTB3 as a decimal rate under the desk's own knowability rule: the
    latest row dated at or before the session BEFORE ``session`` (FRED
    publishes T-1; ``pit.PUBLICATION_LAG_SESSIONS['DTB3'] == 1``). Readable
    the same evening, unlike ``features/<D>.json`` (written 14:30 ET on D+1)."""
    try:
        rows = indices.read_store(store / "indices" / "DTB3.csv")
    except (OSError, ValueError):
        return None, "no DTB3 series in the store"
    limit = previous_session(session, cal)
    if limit is None:
        return None, f"no NYSE session before {session}"
    cut = limit.isoformat()
    closes = {d: c for d, _o, _h, _l, c in rows if d <= cut}
    if not closes:
        return None, f"no DTB3 observation on or before {cut}"
    latest = max(closes)
    try:
        rate = float(closes[latest]) / 100.0
    except (ValueError, OverflowError):
        return None, f"invalid DTB3 close of {latest}"
    # Broad input sanity bound, not a replacement value or strategy parameter.
    # Never fall back to an older rate when the latest known observation is bad.
    if not math.isfinite(rate) or abs(rate) > 1.0:
        return None, f"invalid DTB3 annual rate of {latest}: outside [-1, 1]"
    return rate, f"DTB3 close of {latest} (1-session lag)"


def build_queue(
    session: date,
    *,
    chains: Mapping[str, Path],
    cal: Calendar,
    rate: float,
    rate_source: str = "",
    seals: Mapping[str, Mapping[str, Any]] | None = None,
    available_by: datetime | None = None,
) -> dict[str, Any]:
    """The queue document of one session (pure given its inputs: the only
    reads are D's recorded chain documents and the sealed desk files; no wall
    clock ever enters the payload, so the same inputs give the same bytes).

    The miner/playbook seals are the REAL sealed versions by default
    (``desk.contracts.parse_queue`` re-checks them against
    ``selection.load_config`` / ``playbook.load_playbook`` and refuses anything
    else, so a queue that misstates its seals can never be adopted)."""
    if seals is None:
        cfg, pb = selection.load_config(), desk_playbook.load_playbook()
        seals = {
            "miner": {
                "version": cfg.version,
                "sha256": cfg.sha256,
                "status": cfg.status,
                "ruling": cfg.ruling,
                "n_paths": cfg.n_paths,
            },
            "playbook": {"version": pb.version, "sha256": pb.sha256},
        }
    entry = first_session_after(session, cal)
    assert entry is not None  # the calendar reaches past D
    cutoff = datetime.combine(entry, time(9, 30), tzinfo=ET)
    if available_by is not None:
        if available_by.tzinfo is None:
            raise ValueError("availability cutoff must be timezone-aware")
        cutoff = min(cutoff, available_by)
    rows: list[dict[str, Any]] = []
    refused: Counter[str] = Counter()
    family_pass: Counter[str] = Counter()
    names_read = 0
    inputs_sha: dict[str, str | None] = {}
    document_sha: dict[str, str] = {}
    for name in sorted(chains):
        try:
            doc = surface.read_chain_file(chains[name])
        except (OSError, ValueError) as exc:
            refused["unreadable_chain"] += 1
            inputs_sha[name] = f"unreadable: {exc}"
            continue
        names_read += 1
        document_sha[name] = hashlib.sha256(encode(doc)).hexdigest()
        inputs_sha[name] = doc.get("header", {}).get("raw_sha256")
        source_sha = inputs_sha[name]
        try:
            header = doc["header"]
            event = datetime.fromisoformat(header["source_as_of"])
            available = datetime.fromisoformat(header["fetched_at"])
            if (
                header["session"] != session.isoformat()
                or header["underlying"] != name
                or event.tzinfo is None
                or available.tzinfo is None
                or not event <= available <= cutoff
                or not isinstance(source_sha, str)
                or len(source_sha) != 64
                or any(c not in "0123456789abcdef" for c in source_sha)
            ):
                raise ValueError("chain is unavailable at the decision cutoff")
        except (KeyError, ValueError, TypeError):
            refused["chain_availability_invalid"] += 1
            continue
        spot = surface.chain_spot(doc)
        if spot is None:
            refused["no_underlying_close"] += len(FAMILIES)
            continue
        quotes = surface.chain_quotes(doc, session=session, spot=spot, rate=rate)
        sizes = _sizes(doc)
        expiry = pick_expiry(quotes)
        if expiry is None:
            refused["no_expiry_7_35"] += len(FAMILIES)
            continue
        for family in FAMILIES:
            right = RIGHT_OF[family]
            short_q = pick_short(quotes, expiry, right)
            if short_q is None:
                refused[f"{family}:no_delta_solved"] += 1
                continue
            long_q = pick_long(quotes, expiry, right, short_q.strike)
            if long_q is None:
                refused[f"{family}:no_long_strike"] += 1
                continue
            short, long = _leg_quote(short_q, sizes), _leg_quote(long_q, sizes)
            g = gate(family, short, long)
            if not g.ok:
                for why in g.reasons:
                    refused[f"{family}:{why}"] += 1
                continue
            deadline, _why = deadline_for(short.expiry, entry, cal)
            if deadline is None:
                refused[f"{family}:no_hold_window"] += 1
                continue
            if entry_terms(g, family, abs(long.strike - short.strike)) is None:
                refused[f"{family}:entry_terms_width"] += 1
                continue
            rows.append(
                candidate_row(
                    family,
                    name,
                    short,
                    long,
                    g,
                    session=session,
                    entry=entry,
                    deadline=deadline,
                    rank=0,
                    raw_sha256=str(inputs_sha[name]),
                )
            )
            rows[-1]["qsl"]["event_at"] = event.isoformat()
            rows[-1]["qsl"]["available_at"] = available.isoformat()
            rows[-1]["qsl"]["chain_document_sha256"] = document_sha[name]
            family_pass[family] += 1
    rows.sort(key=lambda row: (row["underlying"], row["row"]))
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return {
        "schema": SCHEMA,
        "session": session.isoformat(),
        "entry_session": entry.isoformat(),
        "decision_cutoff": datetime.combine(entry, time(9, 30), tzinfo=ET).isoformat(),
        "valid_until": datetime.combine(entry, time(11, 30), tzinfo=ET).isoformat(),
        "code": CODE,
        "engine": qsl_engine_manifest(),
        "playbook": {"file": f"{seals['playbook']['version']}.toml", **seals["playbook"]},
        "miner": {"file": f"{seals['miner']['version']}.toml", **seals["miner"]},
        "qsl": {
            "evidence_kind": "SIMULATED EXECUTION",
            "execution_authorized": False,
            "exact_external_economics": False,
            "live_money": False,
            "prereg_id": PREREG_ID,
            "prereg_doc": PREREG_DOC,
            "prereg_sha256": PREREG_SHA256,
            "abs_delta_target": str(ABS_DELTA_TARGET),
            "dte_range": [DTE_MIN, DTE_MAX],
            "width_target": str(WIDTH_TARGET),
            "theta": str(THETA),
            "size_floor": SIZE_FLOOR,
            "oi_floor": OI_FLOOR,
            "cost_convention": "crossing prices at the recorded snapshot, never mid",
            "snapshot_honesty": (
                "recorded after the close (~17:46 ET) where spreads are wider than the "
                "liquid session; conservative on crossing costs; snapshot-crossing "
                "modeled shadow fills, not external executions or a 10:00-ET fill observation"
            ),
            "names_read": names_read,
            "gate_pass": len(rows),
            "family_pass": {f: family_pass.get(f, 0) for f in FAMILIES},
            "refused": dict(sorted(refused.items())),
        },
        "inputs": {
            "chains_read": sorted(chains),
            "chains_raw_sha256": dict(sorted(inputs_sha.items())),
            "chain_document_sha256": dict(sorted(document_sha.items())),
            "dtb3_rate": rate,
            "rate_source": rate_source,
        },
        "rows": {},
        "admissible": rows,
        "surfaced": [],
    }


def chain_files(store: Path, session: date) -> dict[str, Path]:
    """D's recorded chain files (conflict sidecars never feed a candidate)."""
    d = store / "chains" / session.isoformat()
    out: dict[str, Path] = {}
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json.gz")):
        if p.name.endswith(".conflict.json.gz") or p.is_symlink():
            continue
        out[p.name[: -len(".json.gz")]] = p
    return out


def run_qsl(
    *,
    session: date | None,
    now: datetime,
    cal: Calendar,
    dry_run: bool = False,
    out: Path | None = None,
    store: Path | None = None,
) -> QslResult:
    """Build session D's QSL queue (default: the latest completed session).

    ``now`` (injected) picks the default session, bounds observed data
    availability and stamps the stage marker; its timestamp is not serialized
    into the payload. ``dry_run`` writes nothing; otherwise the
    queue is written once to ``out`` or ``<queue dir>/<D>.json`` (a differing
    recomputation is kept beside it as a conflict, exactly like the miner).
    Not ready (exit 3, no queue, no marker): D has no recorded chains, or no
    DTB3 rate is knowable at D."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if session is None:
        d = latest_completed_session(now, cal)
    elif not cal.is_session(session):
        return QslResult(2, "bad_session", f"{session} is not an NYSE session", session)
    elif now < cutoff_instant(session):
        return QslResult(2, "not_closed", f"{session} has not closed yet (16:15 ET)", session)
    else:
        d = session
    store = store or paths.store_root()
    live = not dry_run and out is None
    marker = paths.state_root() / "stages" / d.isoformat() / f"{STAGE}.done.json"
    if live and marker.exists():
        return QslResult(0, "already_done", "", d)
    chains = chain_files(store, d)
    if not chains:
        return QslResult(3, "not_ready", f"no recorded chains for {d}", d)
    rate, rate_source = rate_for(d, store, cal)
    if rate is None:
        return QslResult(3, "not_ready", rate_source, d)
    payload = build_queue(
        d, chains=chains, cal=cal, rate=rate, rate_source=rate_source, available_by=now
    )
    data = encode(payload)
    if dry_run:
        if out is not None:
            atomic_write_bytes(out, data)
        return QslResult(0, "dry_run", "", d, payload, out)
    target = out or queue_dir() / f"{d.isoformat()}.json"
    sha = hashlib.sha256(data).hexdigest()
    if atomic_create_bytes(target, data):
        status = "written"
    elif target.read_bytes() == data:
        status = "exists"
    else:
        conflict = paths.state_root() / "qsl-conflicts" / f"{d.isoformat()}.{sha[:12]}.json"
        atomic_write_bytes(conflict, data)
        return QslResult(
            1,
            "conflict",
            f"{target.name} differs from this run (kept); this run -> {conflict}",
            d,
            payload,
            target,
        )
    if live:
        atomic_write_json(
            marker,
            {
                "session": d.isoformat(),
                "stage": STAGE,
                "done_at": now.isoformat(),
                "queue": str(target),
                "queue_sha256": sha,
                "admissible": len(payload["admissible"]),
                "surfaced": 0,
                "prereg_id": PREREG_ID,
                "prereg_sha256": PREREG_SHA256,
            },
        )
    return QslResult(0, status, "", d, payload, target)
