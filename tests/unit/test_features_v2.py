"""Workstream C: V2 features — momentum family + smoothed liquidity
(M2-proper §3.C).

mom_H = log(c_b / c_{b-H}) over the last H+1 bars, which must occupy
H+1 consecutive calendar sessions (a lapse inside the lookback means the
feature is absent, never imputed); ret_1 IS the aligned 1-session member
of the family. dol_vol_20 = mean close x volume over 20 contiguous bars.
Every feature carries its binding bar's provenance and availability.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tree_options.data.authority import PointInTimeDataset
from tree_options.data.ingest import ingest_snapshot
from tree_options.data.raw import build_payload
from tree_options.schemas.security import SecurityMasterRecord, TickerMappingRecord
from tree_options.synth import generate_world
from tree_options.synth.spec import AlphaSpec, WorldSpec

CODE_SHA = "0" * 40


def _pub(session: date) -> datetime:
    return datetime(session.year, session.month, session.day, 23, 0, tzinfo=UTC)


def _bar(sym: str, session: date, close: str, rec: str) -> dict[str, object]:
    c = Decimal(close)
    return dict(
        vendor_symbol=sym,
        session=session,
        open=str(c),
        high=str(c + Decimal("0.50")),
        low=str(c - Decimal("0.50")),
        close=str(c),
        volume=10_000,
        available_at=_pub(session),
        source_record_id=rec,
    )


def _master(pairs: tuple[tuple[str, str], ...]) -> tuple[SecurityMasterRecord, ...]:
    return tuple(
        SecurityMasterRecord(
            security_id=sid,
            figi=f"BBG000FV2T{idx}",
            cik=f"0000008{idx:03d}",
            listing_start=date(2024, 1, 2),
            listing_end=None,
            exchange="NYSE",
            source="fv2-fixture",
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
        for idx, (sid, ticker) in enumerate(pairs)
    )


def _features_authority(static_calendar, rows, pairs):  # type: ignore[no-untyped-def]
    payload = build_payload(
        provider="fv2-fixture-v1",
        rows=tuple(rows),
        retrieved_at=datetime(2026, 12, 31, 2, 0, tzinfo=UTC),
        known_exclusions=(),
    )
    snapshot = ingest_snapshot(
        payload, _master(pairs), snapshot_id="snap-fv2-001", normalization_code_sha=CODE_SHA
    )
    return PointInTimeDataset(snapshot, static_calendar, universe_id="fv2-universe-v1")


def test_momentum_and_liquidity_values(static_calendar) -> None:  # type: ignore[no-untyped-def]
    sess = static_calendar.sessions()
    start = static_calendar.ordinal(date(2024, 7, 8))
    window = sess[start : start + 25]  # 25 consecutive sessions
    assert len(window) == 25
    rows = [_bar("MOM25", s, str(100 + k), f"FV2-{k:04d}") for k, s in enumerate(window)]
    rows += [_bar("MOM12", s, str(200 + k), f"F2B-{k:04d}") for k, s in enumerate(window[:12])]
    ds = _features_authority(static_calendar, rows, (("SEC-F1", "MOM25"), ("SEC-F2", "MOM12")))
    # decide one session after the 25th bar's session so it is visible
    decision = static_calendar.session_close(sess[start + 25])
    panel = ds.features_as_of(
        decision_at=decision, universe_id="fv2-universe-v1", dataset_snapshot_id="snap-fv2-001"
    )
    by_sec = {r.security_id: {f.feature_name: f for f in r.features} for r in panel}

    f1 = by_sec["SEC-F1"]
    assert f1["ret_1"].value == pytest.approx(124.00 / 123.00 - 1)
    assert f1["mom_1"].value == f1["ret_1"].value
    assert f1["mom_5"].value == pytest.approx(math.log(Decimal("124") / Decimal("119")))
    assert f1["mom_20"].value == pytest.approx(math.log(Decimal("124") / Decimal("104")))
    assert f1["dol_vol_20"].value == pytest.approx(float(Decimal(10_000) * Decimal("114.5")))
    assert f1["dol_vol"].value == pytest.approx(124.00 * 10_000)
    # provenance: the binding (latest) input bar carries each feature
    assert f1["mom_5"].source_record_id == "FV2-0024"
    assert f1["mom_20"].source_record_id == "FV2-0024"
    assert f1["dol_vol_20"].source_record_id == "FV2-0024"
    assert f1["mom_5"].available_at == _pub(window[-1])

    f2 = by_sec["SEC-F2"]
    assert f2["mom_5"].value == pytest.approx(math.log(Decimal("211") / Decimal("206")))
    assert "mom_20" not in f2  # only 12 bars of history
    assert "dol_vol_20" not in f2


def test_lapse_inside_lookback_means_absent(static_calendar) -> None:  # type: ignore[no-untyped-def]
    sess = static_calendar.sessions()
    start = static_calendar.ordinal(date(2024, 7, 8))
    # 8 contiguous bars, one missing session, then 2 more: the last 6
    # bars straddle the hole
    idxs = list(range(start, start + 8)) + [start + 9, start + 10]
    rows = [_bar("HOLEY", sess[i], str(300 + k), f"FHC-{k:04d}") for k, i in enumerate(idxs)]
    ds = _features_authority(static_calendar, rows, (("SEC-F3", "HOLEY"),))
    decision = static_calendar.session_close(sess[start + 11])
    panel = ds.features_as_of(
        decision_at=decision, universe_id="fv2-universe-v1", dataset_snapshot_id="snap-fv2-001"
    )
    feats = {f.feature_name for f in panel[0].features}
    assert "ret_1" in feats  # adjacent bars across the hole exist
    assert "mom_1" in feats
    assert "mom_5" not in feats  # lookback straddles the lapse
    assert "dol_vol_20" not in feats


def test_features_absent_not_imputed_when_history_short(static_calendar) -> None:  # type: ignore[no-untyped-def]
    sess = static_calendar.sessions()
    start = static_calendar.ordinal(date(2024, 7, 8))
    rows = [_bar("SHRT", s, "50.00", f"FST-{k:04d}") for k, s in enumerate(sess[start : start + 2])]
    ds = _features_authority(static_calendar, rows, (("SEC-F4", "SHRT"),))
    decision = static_calendar.session_close(sess[start + 2])
    panel = ds.features_as_of(
        decision_at=decision, universe_id="fv2-universe-v1", dataset_snapshot_id="snap-fv2-001"
    )
    names = {f.feature_name for f in panel[0].features}
    assert names == {"ret_1", "mom_1", "dol_vol"}


def test_visible_bars_fast_path_matches_linear_reference(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """The bisect fast path (monotone publication) must equal the linear
    availability filter on every sampled instant, on fixture AND
    generated-world bars."""
    from tests.fixtures import raw_vendor as rv
    from tests.unit.test_synth_generate import base_spec

    payload = build_payload(
        provider=rv.PROVIDER,
        rows=rv.raw_rows(),
        retrieved_at=rv.RETRIEVED_AT,
        known_exclusions=rv.KNOWN_EXCLUSIONS,
    )
    snapshot = ingest_snapshot(
        payload, rv.m1_master(), snapshot_id="snap-m1-fixture-001", normalization_code_sha=CODE_SHA
    )
    world = generate_world(base_spec(), static_calendar)
    wsnap = ingest_snapshot(
        world.payload, world.master, snapshot_id="snap-fv2-w", normalization_code_sha=CODE_SHA
    )
    for snap, uid in ((snapshot, "pit-master-v1"), (wsnap, "SYNTH-U")):
        ds = PointInTimeDataset(snap, static_calendar, universe_id=uid)
        sessions = static_calendar.sessions()[:170]
        for security_id in {b.security_id for b in snap.bars}:
            all_bars = ds.visible_bars(security_id, datetime(2200, 1, 1, tzinfo=UTC))
            for i, s in enumerate(sessions[::3]):
                instant = static_calendar.session_close(s)
                expected = tuple(b for b in all_bars if b.available_at <= instant)
                assert ds.visible_bars(security_id, instant) == expected


def test_v2_panel_passes_availability_audit(static_calendar) -> None:  # type: ignore[no-untyped-def]
    from tests.unit.test_synth_generate import base_spec
    from tree_options.guards.availability import AvailabilityGuard

    spec = base_spec()
    world = generate_world(spec, static_calendar)
    snapshot = ingest_snapshot(
        world.payload, world.master, snapshot_id=spec.world_id, normalization_code_sha=CODE_SHA
    )
    ds = PointInTimeDataset(snapshot, static_calendar, universe_id="SYNTH-U")
    guard = AvailabilityGuard(static_calendar)
    sessions = static_calendar.sessions()[:160]
    saw_mom = False
    for s in sessions[60:160:10]:
        panel = ds.features_as_of(
            decision_at=guard.decision_instant(s),
            universe_id="SYNTH-U",
            dataset_snapshot_id=spec.world_id,
        )
        result = guard.audit_panel(panel)
        assert result.n_rejected == 0
        for row in result.compliant:
            names = {f.feature_name for f in row.features}
            saw_mom = saw_mom or ("mom_20" in names and "dol_vol_20" in names)
    assert saw_mom, "the v2 family must actually appear on the generated panel"


def test_aligned_family_detects_planted_alpha_but_not_null(
    static_calendar,
) -> None:  # ignore[type-arg]
    """End-to-end alignment pin (plan §3.C): on same-seed same-world-id
    twins, ret_1 (the aligned 1-session family member) ranks next-session
    returns positively in the alpha twin and ~zero in the null twin. A
    deliberately huge coefficient (0.5) makes the unit test's small world
    decisive; the drift wall keeps both twins gate-clean."""
    from tests.unit.test_synth_generate import base_spec

    def _mean_rank_corr(kind: str) -> float:  # type: ignore[no-untyped-def]
        overrides: dict[str, object] = {"kind": kind}
        if kind == "alpha":
            overrides["alpha"] = AlphaSpec(family="linear_momentum", coefficient=0.5)
        spec = base_spec(**overrides)  # type: ignore[arg-type]
        assert isinstance(spec, WorldSpec)
        world = generate_world(spec, static_calendar)
        snapshot = ingest_snapshot(
            world.payload, world.master, snapshot_id=spec.world_id, normalization_code_sha=CODE_SHA
        )
        ds = PointInTimeDataset(snapshot, static_calendar, universe_id="SYNTH-U")
        sessions = static_calendar.sessions()[: spec.n_sessions]  # type: ignore[index]
        closes: dict[str, dict[date, Decimal]] = {}
        ordinals = {s: i for i, s in enumerate(sessions)}
        for bar in snapshot.bars:
            closes.setdefault(bar.security_id, {})[bar.session] = bar.close
        corrs: list[float] = []
        for s in sessions[3:-1]:
            panel = ds.features_as_of(
                decision_at=static_calendar.session_close(s),
                universe_id="SYNTH-U",
                dataset_snapshot_id=spec.world_id,
            )
            pairs: list[tuple[float, float]] = []
            for row in panel:
                feat = next((f for f in row.features if f.feature_name == "mom_1"), None)
                if feat is None:
                    continue
                series = closes[row.security_id]
                o = ordinals[s]
                ret_s = series.get(s)  # decision-session return = label window start
                ret_prev = series.get(sessions[o - 1])
                if ret_s is None or ret_prev is None:
                    continue
                nxt = float(ret_s / ret_prev)
                pairs.append((feat.value, nxt))
            if len(pairs) >= 5:
                corrs.append(_rank_corr(pairs))
        assert corrs
        return sum(corrs) / len(corrs)

    alpha_corr = _mean_rank_corr("alpha")
    null_corr = _mean_rank_corr("null")
    # deterministic on the seeded small world: measured alpha ~0.187 at
    # coefficient 0.5 (the drift wall dampens even a huge planting —
    # evidence the wall bites), null ~0. Thresholds sit well off both.
    assert alpha_corr > 0.12, f"aligned family must detect the planted effect, got {alpha_corr}"
    assert abs(null_corr) < 0.08, f"null twin must sit near zero, got {null_corr}"


def _rank_corr(pairs: list[tuple[float, float]]) -> float:
    """Spearman rank correlation (ties: average ranks) for one session."""

    def _avg_ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    rx, ry = _avg_ranks(xs), _avg_ranks(ys)
    n = len(xs)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return 0.0
    return cov / math.sqrt(vx * vy)
