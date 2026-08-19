"""Workstream B: forward-return label construction (M2-proper §3.B).

Label semantics — the decision-time information boundary convention
(plan correction 2026-08-19): with b = the session of the last bar
VISIBLE at the decision instant, value = ln(close[b+H]/close[b]) as a
total return (ratio events adjust shares by n/d; cash dividends are
held unreinvested), label_window = (b+1, b+H), observed_at = the end
bar's publication instant. Labels are outcomes: never availability
gated, protected by purge instead.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tests.fixtures import raw_vendor as rv
from tree_options.data.authority import PointInTimeDataset
from tree_options.data.ingest import ingest_snapshot
from tree_options.data.raw import build_payload
from tree_options.labels import LabelBuildError, build_labels
from tree_options.splitting.checks import check_folds
from tree_options.splitting.splitter import WalkForwardSplitter
from tree_options.synth import generate_world

SNAPSHOT_ID = "snap-m1-fixture-001"
CODE_SHA = "0" * 40
UNIVERSE_ID = "pit-master-v1"


def _pub(session: date) -> datetime:
    return datetime(session.year, session.month, session.day, 23, 0, tzinfo=UTC)


def _authority(static_calendar):  # type: ignore[no-untyped-def]
    payload = build_payload(
        provider=rv.PROVIDER,
        rows=rv.raw_rows(),
        retrieved_at=rv.RETRIEVED_AT,
        known_exclusions=rv.KNOWN_EXCLUSIONS,
    )
    snapshot = ingest_snapshot(
        payload, rv.m1_master(), snapshot_id=SNAPSHOT_ID, normalization_code_sha=CODE_SHA
    )
    return PointInTimeDataset(snapshot, static_calendar, universe_id=UNIVERSE_ID)


def _by_key(labels):  # type: ignore[no-untyped-def]
    return {(lab.security_id, lab.decision_session): lab for lab in labels}


# --------------------------------------------------------------------------
# exact value/window/provenance on the M1 fixture
# --------------------------------------------------------------------------


def test_h1_label_value_window_and_provenance(static_calendar) -> None:  # type: ignore[no-untyped-def]
    ds = _authority(static_calendar)
    labels = _by_key(
        build_labels(ds, static_calendar, horizon_sessions=1, decision_sessions=[date(2024, 3, 15)])
    )
    lab = labels[("SEC-001", date(2024, 3, 15))]
    # base = last decision-time VISIBLE close (2024-03-14, 52.00): the
    # 03-15 bar publishes at 23:00, after the close
    assert lab.value == pytest.approx(math.log(Decimal("52.50") / Decimal("52.00")))
    assert lab.label_window == (date(2024, 3, 15), date(2024, 3, 15))
    assert lab.horizon_sessions == 1
    assert lab.observed_at == _pub(date(2024, 3, 15))
    assert lab.source == rv.PROVIDER
    assert lab.source_record_id == "RAW-0003"  # the end bar carries provenance
    assert lab.adjustment_source_record_ids == ()


def test_h1_label_across_split_is_total_return(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """2:1 split inside the window: raw 40/80 = 0.5, share-adjusted 1.0."""
    ds = _authority(static_calendar)
    labels = _by_key(
        build_labels(ds, static_calendar, horizon_sessions=1, decision_sessions=[date(2024, 7, 1)])
    )
    lab = labels[("SEC-001", date(2024, 7, 1))]
    assert lab.value == 0.0  # exactly: Decimal(40/80 * 2/1) == 1
    assert lab.label_window == (date(2024, 7, 1), date(2024, 7, 1))
    assert lab.adjustment_source_record_ids == ("ACT-0002",)


def test_h1_label_across_reverse_split_is_total_return(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """1:2 reverse split: raw 20/10 = 2, share-adjusted 1.0."""
    ds = _authority(static_calendar)
    labels = _by_key(
        build_labels(ds, static_calendar, horizon_sessions=1, decision_sessions=[date(2024, 7, 5)])
    )
    lab = labels[("SEC-005", date(2024, 7, 5))]
    assert lab.value == 0.0
    assert lab.adjustment_source_record_ids == ("ACT-0003",)


def test_h1_label_successor_ticker_recycle(static_calendar) -> None:  # type: ignore[no-untyped-def]
    ds = _authority(static_calendar)
    labels = _by_key(
        build_labels(ds, static_calendar, horizon_sessions=1, decision_sessions=[date(2024, 9, 4)])
    )
    lab = labels[("SEC-002", date(2024, 9, 4))]
    assert lab.value == pytest.approx(math.log(Decimal("31.00") / Decimal("30.00")))


def test_no_label_when_window_has_a_gap(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """H=2 from 2024-03-15 needs a bar on 03-18 (none exists in the fixture)."""
    ds = _authority(static_calendar)
    labels = build_labels(
        ds, static_calendar, horizon_sessions=2, decision_sessions=[date(2024, 3, 15)]
    )
    assert labels == ()


def test_no_label_when_stale_history_straddles_decision(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """At 2024-06-28 SEC-001's last visible bar is 03-15: the forward window
    from that stale base cannot be contiguous through the decision."""
    ds = _authority(static_calendar)
    labels = build_labels(
        ds, static_calendar, horizon_sessions=1, decision_sessions=[date(2024, 6, 28)]
    )
    assert labels == ()


def test_no_label_before_first_history_is_visible(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """SEC-002's first bar (2024-09-03) publishes after the close that day."""
    ds = _authority(static_calendar)
    labels = build_labels(
        ds, static_calendar, horizon_sessions=1, decision_sessions=[date(2024, 9, 3)]
    )
    assert labels == ()


def test_every_label_observed_strictly_after_decision(static_calendar) -> None:  # type: ignore[no-untyped-def]
    ds = _authority(static_calendar)
    sessions = static_calendar.sessions()
    labels = build_labels(
        ds, static_calendar, horizon_sessions=1, decision_sessions=sessions[1600:1700]
    )
    assert labels, "fixture must produce labels across a session sweep"
    for lab in labels:
        assert lab.observed_at > static_calendar.session_close(lab.decision_session)


def test_base_bar_equals_last_visible_at_decision(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """The label window starts at the session AFTER the last bar the
    authority itself would return at the decision instant (the builder's
    visibility walk must not drift from the authority's filter)."""
    ds = _authority(static_calendar)
    sessions = static_calendar.sessions()
    labels = build_labels(
        ds, static_calendar, horizon_sessions=1, decision_sessions=sessions[1600:1700]
    )
    for lab in labels:
        dc = static_calendar.session_close(lab.decision_session)
        visible = ds.visible_bars(lab.security_id, dc)
        assert visible, lab
        last = visible[-1]
        assert (
            static_calendar.ordinal(lab.label_window[0])
            == static_calendar.ordinal(last.session) + 1
        )


def test_build_labels_is_deterministic(static_calendar) -> None:  # type: ignore[no-untyped-def]
    ds = _authority(static_calendar)
    sessions = static_calendar.sessions()[2:200:3]
    a = build_labels(ds, static_calendar, horizon_sessions=2, decision_sessions=sessions)
    b = build_labels(ds, static_calendar, horizon_sessions=2, decision_sessions=sessions)
    assert a == b


def test_horizon_must_be_positive(static_calendar) -> None:  # type: ignore[no-untyped-def]
    ds = _authority(static_calendar)
    with pytest.raises(ValueError, match="horizon_sessions"):
        build_labels(ds, static_calendar, horizon_sessions=0, decision_sessions=[date(2024, 3, 15)])


def test_non_session_decision_date_fails_closed(static_calendar) -> None:  # type: ignore[no-untyped-def]
    ds = _authority(static_calendar)
    with pytest.raises(Exception, match="not a session"):
        build_labels(
            ds, static_calendar, horizon_sessions=1, decision_sessions=[date(2024, 7, 6)]
        )  # Saturday


# --------------------------------------------------------------------------
# cash dividends and calendar tail (purpose-built payload)
# --------------------------------------------------------------------------


def _cash_and_tail_authority(static_calendar):  # type: ignore[no-untyped-def]
    from tree_options.schemas.security import (
        SecurityMasterRecord,
        TickerMappingRecord,
    )

    tail = static_calendar.sessions()[-3:]
    rows: list[dict[str, object]] = [
        dict(
            vendor_symbol="CASHW",
            session=date(2024, 7, 8),
            open="100.00",
            high="100.50",
            low="99.50",
            close="100.00",
            volume=10_000,
            available_at=_pub(date(2024, 7, 8)),
            source_record_id="LBL-0001",
        ),
        dict(
            vendor_symbol="CASHW",
            session=date(2024, 7, 9),
            open="99.00",
            high="99.50",
            low="98.50",
            close="99.00",
            volume=10_000,
            available_at=_pub(date(2024, 7, 9)),
            source_record_id="LBL-0002",
        ),
        dict(
            vendor_symbol="CASHW",
            session=date(2024, 7, 10),
            open="98.00",
            high="98.50",
            low="97.50",
            close="98.00",
            volume=10_000,
            available_at=_pub(date(2024, 7, 10)),
            source_record_id="LBL-0003",
        ),
        dict(
            vendor_symbol="TAILE",
            session=tail[0],
            open="50.00",
            high="50.50",
            low="49.50",
            close="50.00",
            volume=10_000,
            available_at=_pub(tail[0]),
            source_record_id="LBL-0004",
        ),
        dict(
            vendor_symbol="TAILE",
            session=tail[1],
            open="51.00",
            high="51.50",
            low="50.50",
            close="51.00",
            volume=10_000,
            available_at=_pub(tail[1]),
            source_record_id="LBL-0005",
        ),
        dict(
            vendor_symbol="TAILE",
            session=tail[2],
            open="52.00",
            high="52.50",
            low="51.50",
            close="52.00",
            volume=10_000,
            available_at=_pub(tail[2]),
            source_record_id="LBL-0006",
        ),
        dict(
            vendor_symbol="CASHW",
            kind="cash_dividend",
            effective_session=date(2024, 7, 9),
            cash_amount="1.00",
            available_at=_pub(date(2024, 7, 8)),
            source_record_id="LBL-ACT1",
        ),
    ]
    master = tuple(
        SecurityMasterRecord(
            security_id=sid,
            figi=f"BBG000LBLT{idx}",
            cik=f"00000009{idx:02d}",
            listing_start=date(2024, 1, 2),
            listing_end=None,
            exchange="NYSE",
            source="label-fixture",
            available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
            ticker_mappings=(
                TickerMappingRecord(
                    security_id=sid,
                    ticker=ticker,
                    effective_from=date(2024, 1, 2),
                    available_at=datetime(2024, 1, 2, 21, 0, tzinfo=UTC),
                ),
            ),
        )
        for idx, (sid, ticker) in enumerate((("SEC-LC1", "CASHW"), ("SEC-LT1", "TAILE")))
    )
    payload = build_payload(
        provider="label-fixture-v1",
        rows=tuple(rows),
        retrieved_at=datetime(2026, 12, 31, 2, 0, tzinfo=UTC),
        known_exclusions=(),
    )
    snapshot = ingest_snapshot(
        payload, master, snapshot_id="snap-label-fixture-001", normalization_code_sha=CODE_SHA
    )
    return PointInTimeDataset(snapshot, static_calendar, universe_id="label-universe-v1")


def test_cash_dividend_held_as_cash_in_value(static_calendar) -> None:  # type: ignore[no-untyped-def]
    ds = _cash_and_tail_authority(static_calendar)
    labels = _by_key(
        build_labels(ds, static_calendar, horizon_sessions=1, decision_sessions=[date(2024, 7, 9)])
    )
    lab = labels[("SEC-LC1", date(2024, 7, 9))]
    # (99 x 1 + 1.00 cash) / 100 = 1.0 exactly
    assert lab.value == 0.0
    assert lab.adjustment_source_record_ids == ("LBL-ACT1",)

    h2 = _by_key(
        build_labels(ds, static_calendar, horizon_sessions=2, decision_sessions=[date(2024, 7, 9)])
    )
    lab2 = h2[("SEC-LC1", date(2024, 7, 9))]
    # (98 + 1.00) / 100 = 0.99
    assert lab2.value == pytest.approx(math.log(Decimal("99") / Decimal("100")))
    assert lab2.label_window == (date(2024, 7, 9), date(2024, 7, 10))


def test_window_past_calendar_end_has_no_label(static_calendar) -> None:  # type: ignore[no-untyped-def]
    ds = _cash_and_tail_authority(static_calendar)
    last = static_calendar.sessions()[-1]
    h1 = _by_key(build_labels(ds, static_calendar, horizon_sessions=1, decision_sessions=[last]))
    assert h1[("SEC-LT1", last)].value == pytest.approx(math.log(Decimal("52") / Decimal("51")))
    h2 = build_labels(ds, static_calendar, horizon_sessions=2, decision_sessions=[last])
    assert h2 == ()


# --------------------------------------------------------------------------
# purge consistency against real fold geometry (synthetic small world)
# --------------------------------------------------------------------------


def test_label_windows_never_touch_fold_eval_blocks(static_calendar) -> None:  # type: ignore[no-untyped-def]
    from tests.unit.test_synth_generate import base_spec

    world = generate_world(base_spec(), static_calendar)
    snapshot = ingest_snapshot(
        world.payload,
        world.master,
        snapshot_id="snap-label-purge-001",
        normalization_code_sha=CODE_SHA,
    )
    ds = PointInTimeDataset(snapshot, static_calendar, universe_id="synth-labels-v1")
    sessions = static_calendar.sessions()[: base_spec().n_sessions or 160]
    labels = build_labels(ds, static_calendar, horizon_sessions=5, decision_sessions=sessions)
    assert labels, "small world must produce labels"

    splitter = WalkForwardSplitter(
        static_calendar,
        label_horizon_sessions=5,
        embargo_sessions=2,
        val_sessions=10,
        test_sessions=10,
        roll_sessions=10,
        min_train_sessions=20,
    )
    folds = splitter.splits()
    assert folds, "small fold params must produce folds on the small world"
    check_folds(folds, calendar=static_calendar, label_horizon_sessions=5, embargo_sessions=2)

    checked_train = 0
    for fold in folds:
        eval_sessions = fold.all_eval_sessions
        test_sessions = fold.test_sessions
        for lab in labels:
            if lab.decision_session not in fold.train_sessions:
                continue
            checked_train += 1
            lo, hi = lab.label_window
            assert not any(lo <= s <= hi for s in eval_sessions), (
                f"fold {fold.fold_id}: train label window {(lo, hi)} touches eval"
            )
            if lab.decision_session in fold.final_fit_train_sessions:
                assert not any(lo <= s <= hi for s in test_sessions), (
                    f"fold {fold.fold_id}: final-fit label window touches test"
                )
    assert checked_train, "some train sessions must carry labels"


def test_non_monotone_publication_fails_closed(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """The two-pointer visibility walk assumes publication order follows
    session order; a series that violates it must fail loudly, not mislabel."""
    ds = _authority(static_calendar)
    original = ds.visible_bars

    def scrambled(security_id: str, decision_at: datetime):  # type: ignore[no-untyped-def]
        bars = original(security_id, decision_at)
        if not bars or security_id != "SEC-001":
            return bars
        # pretend the 06-28 bar was published before the 03-15 bar
        reordered = tuple(sorted(bars, key=lambda b: b.available_at, reverse=True))
        return reordered

    ds.visible_bars = scrambled  # type: ignore[method-assign]
    with pytest.raises(LabelBuildError, match="monotone"):
        build_labels(ds, static_calendar, horizon_sessions=1, decision_sessions=[date(2024, 7, 1)])
