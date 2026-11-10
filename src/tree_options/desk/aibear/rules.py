"""The three AI-BEAR sleeves' entry and exit rules (shared by backtest and monitor).

* core    put debit vertical, ATM / 70 %, 270 DTE, single names and indices;
          opened while the state is TOPPING, BEAR or RELIEF
* falls   put debit vertical, ATM / 90 %, 45 DTE, indices/ETFs; opened on the
          first BEAR close after a RELIEF or TOPPING close (a failed rally or
          a fresh breakdown)
* relief  call debit vertical, ATM / 110 %, 45 DTE, indices/ETFs; opened on
          the first up close right after a CAPITULATION close

Timing: every decision is made at the close of day d from data on or before
d, and executed at the close of d + 1. That applies to entries and exits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from tree_options.desk.aibear.params import NOT_EVALUABLE, STATES
from tree_options.desk.aibear.pricing import Right


@dataclass(frozen=True)
class SleeveRule:
    name: str
    right: Right
    long_moneyness: float
    short_moneyness: float
    dte: int
    take_profit_width_frac: float
    exit_dte: int
    max_hold_sessions: int | None
    entry_states: frozenset[str] | None
    entry_prior_states: frozenset[str] | None
    entry_requires_up_close: bool
    stop_states: frozenset[str]
    regime_stop_bull_sessions: int | None
    cooldown_after_regime_stop: int
    stop_below_episode_low: bool
    instruments: tuple[str, ...]
    #: AI-BEAR-002: an entry also needs a CAPITULATION close within the last N
    #: sessions; the episode-low stop then spans those CAPITULATION closes
    recent_capitulation_within: int | None = None
    #: AI-BEAR-002: stop only on a trend reversal (BULL with a rising 200-day MA)
    stop_on_trend_reversal: bool = False

    @classmethod
    def from_doc(cls, name: str, d: dict[str, Any]) -> SleeveRule:
        def states(key: str) -> frozenset[str] | None:
            if key not in d:
                return None
            got = frozenset(d[key])
            bad = got - set(STATES)
            if bad:
                raise ValueError(f"{name}.{key}: unknown states {sorted(bad)}")
            return got

        right = d["right"]
        if right not in ("C", "P"):
            raise ValueError(f"{name}: right must be C or P")
        long_m, short_m = float(d["long_moneyness"]), float(d["short_moneyness"])
        if (right == "P" and not short_m < long_m) or (right == "C" and not short_m > long_m):
            raise ValueError(f"{name}: the short strike must be further out of the money")
        stop = d.get("stop")
        if stop not in (None, "close_below_episode_low", "trend_reversal"):
            raise ValueError(f"{name}: unknown stop {stop!r}")
        within = d.get("recent_capitulation_within")
        return cls(
            name=name,
            right=right,
            long_moneyness=long_m,
            short_moneyness=short_m,
            dte=int(d["dte"]),
            take_profit_width_frac=float(d["take_profit_width_frac"]),
            exit_dte=int(d["exit_dte"]),
            max_hold_sessions=(
                None if d.get("max_hold_sessions") is None else int(d["max_hold_sessions"])
            ),
            entry_states=states("entry_states"),
            entry_prior_states=states("entry_prior_states"),
            entry_requires_up_close=bool(d.get("entry_requires_up_close", False)),
            stop_states=states("stop_states") or frozenset(),
            regime_stop_bull_sessions=(
                None
                if d.get("regime_stop_bull_sessions") is None
                else int(d["regime_stop_bull_sessions"])
            ),
            cooldown_after_regime_stop=int(d.get("cooldown_after_regime_stop", 0)),
            stop_below_episode_low=stop == "close_below_episode_low",
            instruments=tuple(d["instruments"]),
            recent_capitulation_within=None if within is None else int(within),
            stop_on_trend_reversal=stop == "trend_reversal",
        )


def sleeve_rules(doc: dict[str, Any]) -> dict[str, SleeveRule]:
    return {name: SleeveRule.from_doc(name, d) for name, d in doc["sleeves"].items()}


def entry_signal(rule: SleeveRule, states: Sequence[str], closes: Sequence[float], i: int) -> bool:
    """Does the close of day ``i`` signal an entry (executed at ``i + 1``)?"""
    s = states[i]
    if s == NOT_EVALUABLE:
        return False
    if rule.entry_states is not None and s not in rule.entry_states:
        return False
    if rule.entry_prior_states is not None:
        if i < 1 or states[i - 1] not in rule.entry_prior_states:
            return False
    if rule.entry_requires_up_close and not (i >= 1 and closes[i] > closes[i - 1]):
        return False
    n = rule.recent_capitulation_within
    if n is not None and "CAPITULATION" not in states[max(0, i - n) : i]:
        return False
    return True


def recent_capitulation_low(
    states: Sequence[str], closes: Sequence[float], i: int, within: int
) -> float | None:
    """Lowest close among the CAPITULATION closes in ``[i - within, i - 1]``."""
    lows = [closes[k] for k in range(max(0, i - within), i) if states[k] == "CAPITULATION"]
    return min(lows) if lows else None


def stop_level(
    rule: SleeveRule, states: Sequence[str], closes: Sequence[float], i: int
) -> float | None:
    """The episode-low stop for a position signalled at ``i`` (None: no such stop)."""
    if not rule.stop_below_episode_low:
        return None
    if rule.recent_capitulation_within is not None:
        return recent_capitulation_low(states, closes, i, rule.recent_capitulation_within)
    return episode_low(states, closes, i)


def episode_low(states: Sequence[str], closes: Sequence[float], i: int) -> float | None:
    """Lowest close of the CAPITULATION run that ends at ``i - 1`` (the relief
    sleeve's stop level), or None when ``i - 1`` is not CAPITULATION."""
    j = i - 1
    if j < 0 or states[j] != "CAPITULATION":
        return None
    low = closes[j]
    while j - 1 >= 0 and states[j - 1] == "CAPITULATION":
        j -= 1
        low = min(low, closes[j])
    return low


def bull_runs(states: Sequence[str]) -> list[int]:
    """``out[j]`` = consecutive BULL closes ending at ``j`` (0 when not BULL)."""
    out: list[int] = []
    run = 0
    for s in states:
        run = run + 1 if s == "BULL" else 0
        out.append(run)
    return out


def exit_reason(
    rule: SleeveRule,
    *,
    states: Sequence[str],
    closes: Sequence[float],
    j: int,
    bull_run: int,
    held_sessions: int,
    dte_left: int,
    value_mid: float,
    width: float,
    stop_low: float | None,
    use_regime_stop: bool = True,
    trend_up: bool = False,
) -> str | None:
    """The exit decided at the close of ``j``, or None to keep holding.
    Checked in a fixed order: take-profit, stops, then time. ``bull_run`` is
    ``bull_runs(states)[j]``; ``trend_up`` is "the 200-day MA is above its
    value 20 sessions earlier" at ``j`` (only the trend-reversal stop reads it)."""
    if value_mid >= rule.take_profit_width_frac * width:
        return "take_profit"
    if use_regime_stop and states[j] in rule.stop_states:
        return "regime_stop"
    if use_regime_stop and rule.stop_on_trend_reversal and states[j] == "BULL" and trend_up:
        return "trend_reversal_stop"
    if (
        use_regime_stop
        and rule.regime_stop_bull_sessions is not None
        and bull_run >= rule.regime_stop_bull_sessions
    ):
        return "regime_stop"
    if rule.stop_below_episode_low and stop_low is not None and closes[j] < stop_low:
        return "episode_low_stop"
    if rule.max_hold_sessions is not None and held_sessions >= rule.max_hold_sessions:
        return "time_stop"
    if dte_left <= rule.exit_dte:
        return "dte_exit"
    return None
