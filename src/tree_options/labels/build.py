"""Forward-return label construction over the point-in-time authority
(M2-proper §3.B, plan correction 2026-08-19).

Semantics — the decision-time information boundary: with b = the session
of the last bar VISIBLE at the decision instant (decision_instant =
session_close(d); the 23:00-UTC publication postdates the close, so on
punctual vendors b is the session immediately before d),

    value        = ln( (close[b+H] * R + cash) / close[b] )
    label_window = (b+1, b+H)

R = product over ratio events (split / reverse_split / stock_dividend)
with effective session inside (b, b+H] of n/d — the SHARE multiplier
that cancels the price jump the generator derives as close * d/n — and
`cash` = the sum of cash dividends in the window, held unreinvested.
The window is strictly post-decision-knowledge: every window bar
publishes after the decision instant (b is the last visible session and
publication order follows session order, enforced below), so the label
carries no return that was knowable at decision time.

Labels are OUTCOMES: never availability-gated (INV-06 purge protects
them instead — proven by test against real fold geometry). Absent, never
imputed, when: nothing is visible at decision; the forward window runs
past the calendar; the bar series is not contiguous over the window
(lapse, delisting); or the security is not in the point-in-time universe
that session. Provenance: the end bar (its publication is what makes the
value knowable) plus the source_record_id of every adjusting action.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise

from tree_options.data.actions import CorporateActionRecord
from tree_options.data.authority import PointInTimeDataset
from tree_options.schemas.features import LabelEvent
from tree_options.time.calendar import SessionCalendar

# Named intent: an instant after every publication, so `visible_bars`
# returns the security's EVENTUAL full series (labels are outcomes and
# legitimately consume post-decision bars through the authority's own
# read gate).
_EVENTUAL = datetime(2200, 1, 1, tzinfo=UTC)

_RATIO_KINDS = frozenset({"split", "reverse_split", "stock_dividend"})


class LabelBuildError(ValueError):
    """The bar series violates an assumption label construction relies on."""


def build_labels(
    dataset: PointInTimeDataset,
    calendar: SessionCalendar,
    *,
    horizon_sessions: int,
    decision_sessions: Iterable[date],
) -> tuple[LabelEvent, ...]:
    """Build forward H-session total-return labels for the PIT universe."""
    if horizon_sessions < 1:
        raise ValueError(f"horizon_sessions must be >= 1, got {horizon_sessions}")

    sessions = calendar.sessions()
    n_sessions = len(sessions)
    ordinal = {s: i for i, s in enumerate(sessions)}

    decision_dates = sorted(set(decision_sessions))
    for d in decision_dates:
        if not calendar.is_session(d):
            raise ValueError(f"decision session {d} is not a session in this calendar")
    universe = {
        d: frozenset(dataset.universe_as_of(calendar.session_close(d))) for d in decision_dates
    }
    securities = sorted(frozenset().union(*universe.values())) if universe else []

    # outcome-side adjustment facts, indexed by effective-session ordinal
    actions_by_security: dict[str, list[tuple[int, CorporateActionRecord]]] = {}
    for act in dataset.actions:
        if act.kind not in _RATIO_KINDS and act.kind != "cash_dividend":
            continue
        if act.effective_session not in ordinal:
            continue
        actions_by_security.setdefault(act.security_id, []).append(
            (ordinal[act.effective_session], act)
        )

    labels: list[LabelEvent] = []
    for security_id in securities:
        bars = dataset.visible_bars(security_id, _EVENTUAL)
        if not bars:
            continue
        # fail closed: the two-pointer visibility walk below assumes
        # publication order follows session order (r0 data: one
        # publication per bar, at the session's vendor instant)
        for earlier, later in pairwise(bars):
            if later.available_at < earlier.available_at:
                raise LabelBuildError(
                    f"{security_id}: available_at not monotone with session order "
                    f"({earlier.session} -> {later.session}) — visibility walk refused"
                )
        bar_at_ordinal = {ordinal[bar.session]: bar for bar in bars}
        adjustments = actions_by_security.get(security_id, [])

        visible_upto = 0  # first index not yet visible at the current decision instant
        for d in decision_dates:
            if security_id not in universe[d]:
                continue
            decision_at = calendar.session_close(d)
            decision_ordinal = ordinal[d]
            while visible_upto < len(bars) and bars[visible_upto].available_at <= decision_at:
                visible_upto += 1
            if visible_upto == 0:
                continue  # nothing visible at decision
            base = bars[visible_upto - 1]
            base_ordinal = ordinal[base.session]
            if base_ordinal + 1 != decision_ordinal:
                continue  # stale history does not reach the decision boundary
            end_ordinal = base_ordinal + horizon_sessions
            if end_ordinal >= n_sessions:
                continue  # window runs past the calendar
            if any(o not in bar_at_ordinal for o in range(base_ordinal + 1, end_ordinal + 1)):
                continue  # gap inside the window (lapse / delisting)
            end = bar_at_ordinal[end_ordinal]

            ratio_factor = Decimal(1)
            cash_total = Decimal(0)
            adjusted_ids: list[str] = []
            # walk in effective-session order so the running share count is
            # correct when a cash dividend follows a ratio action inside the
            # window (review r1 P1-1): the dividend accrues on the shares
            # held when it is PAID, i.e. scaled by the ratio factor accrued
            # so far
            latest_input_pub = end.available_at
            for action_ordinal, act in sorted(adjustments, key=lambda pair: pair[0]):
                if base_ordinal < action_ordinal <= end_ordinal:
                    adjusted_ids.append(act.source_record_id)
                    if act.available_at > latest_input_pub:
                        # the value embeds this action, so the label is not
                        # observable before the action itself publishes
                        # (review r1 P1-2)
                        latest_input_pub = act.available_at
                    if act.kind in _RATIO_KINDS:
                        if act.ratio_numerator is None or act.ratio_denominator is None:
                            raise LabelBuildError(
                                f"{act.source_record_id}: {act.kind} without a ratio"
                            )
                        ratio_factor *= Decimal(act.ratio_numerator) / Decimal(
                            act.ratio_denominator
                        )
                    else:
                        if act.cash_amount is None:
                            raise LabelBuildError(
                                f"{act.source_record_id}: {act.kind} without a cash amount"
                            )
                        cash_total += act.cash_amount * ratio_factor

            wealth = (end.close * ratio_factor + cash_total) / base.close
            labels.append(
                LabelEvent(
                    security_id=security_id,
                    decision_session=d,
                    horizon_sessions=horizon_sessions,
                    label_window=(sessions[base_ordinal + 1], sessions[end_ordinal]),
                    value=math.log(float(wealth)),
                    observed_at=latest_input_pub,
                    source=end.source,
                    source_record_id=end.source_record_id,
                    adjustment_source_record_ids=tuple(sorted(adjusted_ids)),
                )
            )
    return tuple(labels)
