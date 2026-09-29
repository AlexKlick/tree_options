"""Theory rule builtins for the desk long run (config ``"builtin": "theory"``).

Deterministic, board-only rules for pre-registered market-structure hypotheses (the theory
lane, ``~/.local/state/trex-theory/market``, 2026-09-28). A rule reads nothing but the
:class:`~tree_options.desk.longrun.Board` every arm sees: the v2 rows and the public as-of
context. A config entry names the knobs, e.g. the direction-balanced short-vol stand-in::

    {"name": "theory_shortvol_alt_h5", "kind": "control", "builtin": "theory",
     "structures": ["put_credit", "call_credit"], "require_all": true,
     "alternate": "slot", "horizon": "hold:5"}

Knobs (only ``structures`` is required):

- ``structures``: the structures a row may have (a subset of the four);
- ``direction``: ``any`` | ``bullish`` | ``bearish`` | ``momentum_20s`` (the row's direction
  agrees with the sign of its underlying's ``ret_20s_pct``) | ``reversal_1s`` (it disagrees
  with the sign of ``ret_1s_pct``); a null or zero return makes the row ineligible.
  Cross-sectional (relative strength across the board's underlyings, 2026-09-29: the v2
  long run's M3.1 alpha was mostly WHICH underlying it traded): ``xs_weak_20s`` /
  ``xs_weak_5s`` (a bearish row on the underlying with the strictly lowest ``ret_20s_pct``
  / ``ret_5s_pct``) and ``xs_strong_20s`` / ``xs_strong_5s`` (a bullish row on the strictly
  highest); fewer than two underlyings with that return, or a tie at the extreme, makes
  every row ineligible;
- entry filters: ``time_of_day`` (a list of the context's buckets) and inclusive bounds
  ``dte_min``/``dte_max``, ``otm_min``/``otm_max`` (short-strike distance signed toward out
  of the money, percent: +moneyness for call spreads, -moneyness for put spreads),
  ``rv_min``/``rv_max`` (the row underlying's ``rv_20s_ann_pct``) and
  ``max_loss_min``/``max_loss_max``; a row missing a bounded field is ineligible;
- ``require_all``: trade only an underlying on which EVERY listed structure has an eligible
  row (the one-row stand-in for a multi-leg package);
- ``alternate``: ``slot`` rotates the traded structure through ``structures`` by the board's
  slot (calendar ordinal + 45-minute clock index): with ``require_all`` the arm is
  direction-balanced in aggregate by construction, never by the board's content;
- ``key``: the row order among eligible rows (``board_order``, ``max_reward_risk``,
  ``min_max_loss``, ``max_max_loss``, ``max_otm``, ``min_otm``; missing values last, ties
  by board order);
- ``horizon``: the exit mode the rule asks for (``None``: the outcome plug-in's default).

An unknown knob is refused (a typo must not silently become a different arm). A multi-leg
package (put_credit + call_credit on one underlying, the beta-neutral short-vol trade
itself) is NOT expressible: the harness takes one row per board.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tree_options.desk.longrun import Board, Choice

STRUCTURES = ("put_credit", "call_debit", "put_debit", "call_credit")
BULLISH = frozenset({"put_credit", "call_debit"})
#: outcomes.EXIT_MODES (kept local: this module must not import the outcome engine)
HORIZONS = ("intraday", "eod", "hold:1", "hold:3", "hold:5", "hold:10", "expiry")
DIRECTIONS = ("any", "bullish", "bearish", "momentum_20s", "reversal_1s",
              "xs_weak_20s", "xs_strong_20s", "xs_weak_5s", "xs_strong_5s")
KEYS = ("board_order", "max_reward_risk", "min_max_loss", "max_max_loss", "max_otm", "min_otm")
TIMES_OF_DAY = ("open", "morning", "midday", "afternoon", "close")
BOUNDED = ("dte", "otm", "rv", "max_loss")
ALTERNATES = ("slot",)
SLOT_MINUTES = 45
_EPOCH = date(1970, 1, 1)  # day index by subtraction (no ordinal arithmetic in src)
_ENTRY_KEYS = frozenset({"name", "kind", "builtin", "repeats", "structures", "horizon",
                         "direction", "alternate", "require_all", "time_of_day", "key",
                         *(f"{b}_{end}" for b in BOUNDED for end in ("min", "max"))})


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def row_bullish(row: Mapping[str, Any]) -> bool:
    """The row's ``direction`` when given, else its structure's direction."""
    direction = row.get("direction")
    if direction is not None:
        return str(direction).lower() == "bullish"
    return row.get("structure") in BULLISH


def otm_pct(row: Mapping[str, Any]) -> float | None:
    """Short-strike distance toward out of the money, percent (> 0 = OTM)."""
    moneyness = _num(row.get("short_strike_moneyness_pct"))
    if moneyness is None:
        return None
    return moneyness if str(row.get("structure", "")).startswith("call") else -moneyness


def slot_index(session: str, clock: str) -> int:
    """Calendar ordinal + 45-minute clock index: consecutive decision clocks of a day
    are consecutive integers, and the same clock moves by one per calendar day."""
    hours, minutes = clock.split(":")
    return (date.fromisoformat(session) - _EPOCH).days \
        + (int(hours) * 60 + int(minutes)) // SLOT_MINUTES


def _context(board: Board, row: Mapping[str, Any], field: str) -> float | None:
    per = ((board.context or {}).get("underlyings") or {}).get(row.get("underlying")) or {}
    return _num(per.get(field))


@dataclass(frozen=True)
class TheoryParams:
    structures: tuple[str, ...]
    horizon: str | None = None
    direction: str = "any"
    alternate: str | None = None
    require_all: bool = False
    time_of_day: tuple[str, ...] | None = None
    #: (field, low, high) inclusive bounds, field in BOUNDED
    bounds: tuple[tuple[str, float | None, float | None], ...] = ()
    key: str = "board_order"

    def __post_init__(self) -> None:
        if not self.structures or len(set(self.structures)) != len(self.structures) \
                or any(s not in STRUCTURES for s in self.structures):
            raise ValueError(f"structures must be distinct members of {STRUCTURES}")
        if self.horizon is not None and self.horizon not in HORIZONS:
            raise ValueError(f"horizon must be one of {HORIZONS}")
        if self.direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        if self.alternate is not None and self.alternate not in ALTERNATES:
            raise ValueError(f"alternate must be one of {ALTERNATES}")
        if self.key not in KEYS:
            raise ValueError(f"key must be one of {KEYS}")
        if self.time_of_day is not None and (
                not self.time_of_day or any(t not in TIMES_OF_DAY for t in self.time_of_day)):
            raise ValueError(f"time_of_day must be a non-empty subset of {TIMES_OF_DAY}")
        for field, low, high in self.bounds:
            if field not in BOUNDED:
                raise ValueError(f"bounded fields are {BOUNDED}")
            if low is not None and high is not None and low > high:
                raise ValueError(f"{field}: min above max")

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> TheoryParams:
        unknown = sorted(set(entry) - _ENTRY_KEYS)
        if unknown:
            raise ValueError(f"{entry.get('name')}: unknown theory knobs {unknown}")
        structures = entry.get("structures")
        if not isinstance(structures, list | tuple):
            raise ValueError(f"{entry.get('name')}: structures must be a list")
        tod = entry.get("time_of_day")
        bounds = []
        for field in BOUNDED:
            low, high = entry.get(f"{field}_min"), entry.get(f"{field}_max")
            if low is not None or high is not None:
                bounds.append((field, None if low is None else float(low),
                               None if high is None else float(high)))
        horizon = entry.get("horizon")
        alternate = entry.get("alternate")
        return cls(structures=tuple(str(s) for s in structures),
                   horizon=None if horizon is None else str(horizon),
                   direction=str(entry.get("direction", "any")),
                   alternate=None if alternate is None else str(alternate),
                   require_all=bool(entry.get("require_all", False)),
                   time_of_day=None if tod is None else tuple(str(t) for t in tod),
                   bounds=tuple(bounds), key=str(entry.get("key", "board_order")))


def _field(board: Board, row: Mapping[str, Any], field: str) -> float | None:
    if field == "dte":
        return _num(row.get("dte"))
    if field == "otm":
        return otm_pct(row)
    if field == "rv":
        return _context(board, row, "rv_20s_ann_pct")
    return _num(row.get("max_loss"))


def xs_extreme(board: Board, field: str, *, lowest: bool) -> str | None:
    """The underlying with the strictly lowest (highest) ``field`` across the board's
    context; None with fewer than two known values or a tie at the extreme."""
    per = (board.context or {}).get("underlyings") or {}
    known = sorted((v, u) for u, doc in per.items()
                   if (v := _num((doc or {}).get(field))) is not None)
    if len(known) < 2:
        return None
    first, second = (known[0], known[1]) if lowest else (known[-1], known[-2])
    return None if first[0] == second[0] else str(first[1])


def _direction_ok(board: Board, row: Mapping[str, Any], direction: str) -> bool:
    if direction == "any":
        return True
    bullish = row_bullish(row)
    if direction == "bullish":
        return bullish
    if direction == "bearish":
        return not bullish
    if direction.startswith("xs_"):
        _, side, window = direction.split("_")
        weak = side == "weak"
        if bullish == weak:  # weak -> bearish rows only; strong -> bullish rows only
            return False
        return row.get("underlying") == xs_extreme(board, f"ret_{window}_pct", lowest=weak)
    ret = _context(board, row, "ret_20s_pct" if direction == "momentum_20s" else "ret_1s_pct")
    if ret is None or ret == 0:
        return False
    return bullish == (ret > 0) if direction == "momentum_20s" else bullish != (ret > 0)


def _order(key: str) -> Callable[[tuple[int, dict[str, Any]]], tuple[float, int]]:
    def value(row: Mapping[str, Any]) -> float | None:
        if key == "max_reward_risk":
            return _num(row.get("reward_risk"))
        if key in ("min_max_loss", "max_max_loss"):
            return _num(row.get("max_loss"))
        return otm_pct(row)

    def order(item: tuple[int, dict[str, Any]]) -> tuple[float, int]:
        if key == "board_order":
            return 0.0, item[0]
        v = value(item[1])
        if v is None:
            return math.inf, item[0]
        return (-v if key.startswith("max_") else v), item[0]
    return order


def rule_theory(entry: Mapping[str, Any] | TheoryParams) -> Callable[[Board], Choice]:
    """The deterministic rule a ``"builtin": "theory"`` entry names."""
    params = entry if isinstance(entry, TheoryParams) else TheoryParams.from_entry(entry)
    order = _order(params.key)

    def eligible(board: Board, row: dict[str, Any]) -> bool:
        if row.get("structure") not in params.structures:
            return False
        if not _direction_ok(board, row, params.direction):
            return False
        for field, low, high in params.bounds:
            value = _field(board, row, field)
            if value is None or (low is not None and value < low) \
                    or (high is not None and value > high):
                return False
        return True

    def rule(board: Board) -> Choice:
        if params.time_of_day is not None and \
                (board.context or {}).get("time_of_day") not in params.time_of_day:
            return None, None
        rows = [(i, row) for i, row in enumerate(board.rows) if eligible(board, row)]
        if params.require_all:
            held: dict[Any, set[str]] = {}
            for _, row in rows:
                held.setdefault(row.get("underlying"), set()).add(str(row["structure"]))
            full = {u for u, have in held.items() if set(params.structures) <= have}
            rows = [(i, row) for i, row in rows if row.get("underlying") in full]
        if params.alternate == "slot":
            target = params.structures[slot_index(board.session, board.clock)
                                       % len(params.structures)]
            rows = [(i, row) for i, row in rows if row["structure"] == target]
        if not rows:
            return None, None
        return str(min(rows, key=order)[1]["id"]), params.horizon
    return rule
