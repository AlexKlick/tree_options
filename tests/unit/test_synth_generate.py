"""Workstream B: synthetic world generator v1 (M2 packet §3.B).

The generator is a vendor: it emits RawPayload rows through the unchanged
M1 ingest → manifest → authority pipeline. Determinism is the contract
(same spec ⇒ byte-identical payload ⇒ identical content_sha256), the
truth sidecar must be unreachable outside synth/, and every generated
world must be quality-gate clean.
"""

from __future__ import annotations

import ast
import math
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tree_options.data.ingest import ingest_snapshot
from tree_options.data.quality import DataQualityError, verify_manifest
from tree_options.schemas.security import SecurityMasterRecord
from tree_options.synth import ActionRates, AlphaSpec, WorldSpec, generate_world


def base_spec(**overrides: object) -> WorldSpec:
    """A small gate-speed world over the first 160 calendar sessions."""
    rates = overrides.pop(
        "rates",
        ActionRates(
            split=0.5,
            reverse_split=0.2,
            cash_dividend=1.0,
            stock_dividend=0.0,
            rename=0.5,
            merger=0.3,
            bankruptcy=0.2,
            voluntary_delisting=0.2,
            coverage_lapse=0.3,
            ipo_per_year=12,
        ),
    )
    defaults: dict[str, object] = {
        "world_id": "synth-v1-test-001",
        "seed": 20260818,
        "kind": "null",
        "n_securities": 24,
        "n_sessions": 160,
        "rates": rates,
    }
    defaults.update(overrides)
    return WorldSpec(**defaults)  # type: ignore[arg-type]


def _pub(session: date) -> datetime:
    return datetime(session.year, session.month, session.day, 23, 0, tzinfo=UTC)


def test_same_spec_is_byte_identical(static_calendar) -> None:  # type: ignore[no-untyped-def]
    spec = base_spec()
    a = generate_world(spec, static_calendar)
    b = generate_world(spec, static_calendar)
    assert a.payload == b.payload
    assert a.master == b.master
    assert a.truth == b.truth
    # byte-level, not just model equality: canonical serialization of every
    # record must match (Decimal repr drift would pass == but not this)
    from tree_options.data.digest import canonical_bytes

    for x, y in zip(a.payload.bars, b.payload.bars, strict=True):
        assert canonical_bytes(x) == canonical_bytes(y)
    for x, y in zip(a.payload.actions, b.payload.actions, strict=True):
        assert canonical_bytes(x) == canonical_bytes(y)
    for x, y in zip(a.master, b.master, strict=True):
        assert canonical_bytes(x) == canonical_bytes(y)
    # NOTE: snapshot_id is part of every row's canonical bytes (identity
    # binding), so the SAME id must be used to compare content hashes.
    snap_a = ingest_snapshot(
        a.payload, a.master, snapshot_id="det", normalization_code_sha="0" * 64
    )
    snap_b = ingest_snapshot(
        b.payload, b.master, snapshot_id="det", normalization_code_sha="0" * 64
    )
    assert snap_a.manifest.content_sha256 == snap_b.manifest.content_sha256


def test_different_seed_diverges(static_calendar) -> None:  # type: ignore[no-untyped-def]
    a = generate_world(base_spec(seed=1), static_calendar)
    b = generate_world(base_spec(seed=2), static_calendar)
    assert a.payload != b.payload


def test_generated_world_ingests_and_verifies(static_calendar) -> None:  # type: ignore[no-untyped-def]
    spec = base_spec()
    world = generate_world(spec, static_calendar)
    snapshot = ingest_snapshot(
        world.payload,
        world.master,
        snapshot_id=spec.world_id,
        normalization_code_sha="0" * 64,
    )
    verify_manifest(snapshot, static_calendar)
    assert snapshot.manifest.provider == "synthetic/v1"
    assert snapshot.manifest.schema_version == "m2/1"
    assert snapshot.bars, "world must contain bars"
    assert snapshot.actions, "boosted rates must produce actions"


def test_publication_instant_discipline(static_calendar) -> None:  # type: ignore[no-untyped-def]
    world = generate_world(base_spec(), static_calendar)
    for bar in world.payload.bars:
        assert bar.available_at == _pub(bar.session), "bar available_at = 23:00 UTC same session"
    for action in world.payload.actions:
        available = action.available_at
        assert available == _pub(available.date()), "action announced at a pub instant"
        assert available < _pub(action.effective_session), "actions announce before effective"


def test_future_bars_invisible_in_authority(static_calendar) -> None:  # type: ignore[no-untyped-def]
    from tree_options.data.authority import PointInTimeDataset

    spec = base_spec()
    world = generate_world(spec, static_calendar)
    snapshot = ingest_snapshot(
        world.payload,
        world.master,
        snapshot_id=spec.world_id,
        normalization_code_sha="0" * 64,
    )
    ds = PointInTimeDataset(snapshot, static_calendar, universe_id="SYNTH-U")
    sessions = static_calendar.sessions()
    mid = sessions[100]
    decision = _pub(mid)
    universe = ds.universe_as_of(decision)
    for sid in universe:
        visible = ds.visible_bars(sid, decision)
        assert all(b.session <= mid for b in visible), f"future bar visible for {sid}"
    assert universe, "listed names exist mid-world"
    # a security listed mid-world is absent from an earlier universe
    later_listers = [r for r in world.master if r.listing_start > sessions[10]]
    if later_listers:  # pragma: no cover - IPO draw dependent
        early = _pub(sessions[10])
        early_ids = ds.universe_as_of(early)
        assert all(r.security_id not in early_ids for r in later_listers)


def test_null_and_alpha_worlds_share_structure(static_calendar) -> None:  # type: ignore[no-untyped-def]
    null_world = generate_world(base_spec(), static_calendar)
    alpha_world = generate_world(
        base_spec(kind="alpha", alpha=AlphaSpec(family="linear_momentum", coefficient=0.002)),
        static_calendar,
    )
    assert alpha_world.truth.alpha is not None
    assert alpha_world.truth.alpha.coefficient == 0.002
    assert null_world.truth.alpha is None
    # same seed: identical seats/sectors/tickers/events/volumes, different closes
    assert [s.security_id for s in alpha_world.master] == [s.security_id for s in null_world.master]
    assert alpha_world.truth.sector_of == null_world.truth.sector_of
    assert alpha_world.truth.events == null_world.truth.events
    assert [b.volume for b in alpha_world.payload.bars] == [
        b.volume for b in null_world.payload.bars
    ]
    assert [b.session for b in alpha_world.payload.bars] == [
        b.session for b in null_world.payload.bars
    ]
    assert alpha_world.payload.bars != null_world.payload.bars, "planted effect must move closes"


def test_truth_sidecar_import_boundary() -> None:
    """Ground truth is unreachable from feature-construction code: no module
    outside tree_options.synth.* may import synth AT ALL (a bare synth import
    exposes generate_world(...).truth), and nothing outside synth may import
    synth.truth directly."""
    src_root = Path(__file__).resolve().parents[2] / "src" / "tree_options"
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        module = ".".join(path.relative_to(path.parents[2]).with_suffix("").parts)
        if module.startswith("tree_options.synth"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                targets = [node.module]
                if node.level == 0 and node.module == "tree_options.synth":
                    targets += [f"tree_options.synth.{alias.name}" for alias in node.names]
            for target in targets:
                if target == "tree_options.synth" or target.startswith("tree_options.synth."):
                    offenders.append(f"{module} imports {target}")
    assert not offenders, f"synth (incl. truth) leaked into: {offenders}"


def test_every_security_carries_sector(static_calendar) -> None:  # type: ignore[no-untyped-def]
    world = generate_world(base_spec(), static_calendar)
    assert world.master
    for record in world.master:
        assert isinstance(record, SecurityMasterRecord)
        assert record.sector_mappings, f"{record.security_id} has no sector"
        assert record.sector_on(record.listing_start, as_of=_pub(record.listing_start))


def test_lifecycle_scenarios_present(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """All nine M1 fixture scenarios must be reachable with boosted rates —
    and OWNED by the delivered payload/master, not just by the truth sidecar."""
    spec = base_spec(n_securities=40)
    world = generate_world(spec, static_calendar)
    snapshot = ingest_snapshot(
        world.payload,
        world.master,
        snapshot_id=spec.world_id,
        normalization_code_sha="0" * 64,
    )
    kinds = {e.kind for e in world.truth.events}
    expected = {
        "split",
        "reverse_split",
        "cash_dividend",
        "rename",
        "merger",
        "bankruptcy_11",
        "voluntary_delisting",
        "coverage_lapse",
    }
    missing = expected - kinds
    assert not missing, f"missing lifecycle events: {missing}"
    # payload ownership of the ratio/dividend/merger events
    action_kinds = {a.kind for a in snapshot.actions}
    assert {"split", "reverse_split", "cash_dividend", "merger"} <= action_kinds
    # master ownership of the terminal delistings (reasons pinned)
    delist_reasons = {r.delisting.reason for r in world.master if r.delisting is not None}
    assert {"bankruptcy_11", "merger", "voluntary_delisting"} <= delist_reasons
    sessions = static_calendar.sessions()
    later = [r for r in world.master if r.listing_start > sessions[0]]
    assert later, "IPO scenario: later-cohort listings must exist"
    assert world.truth.recycled_tickers, "ticker recycle scenario must occur"


def test_hostile_rate_spec_stays_gate_clean(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """Round-1 P1-2: ANY spec the validator accepts must generate a
    gate-clean world. A split rate of 200/yr drives prices to the floor in
    sessions — ratio events that cannot honor their declared ratio within
    the 2% gate tolerance must be suppressed, not emitted."""
    from tree_options.synth.spec import ActionRates

    spec = base_spec(
        world_id="synth-v1-test-hostile",
        rates=ActionRates(
            split=200.0,
            reverse_split=0.0,
            cash_dividend=0.0,
            stock_dividend=0.0,
            rename=0.0,
            merger=0.0,
            bankruptcy=0.0,
            voluntary_delisting=0.0,
            coverage_lapse=0.0,
            ipo_per_year=0.0,
        ),
    )
    world = generate_world(spec, static_calendar)
    snapshot = ingest_snapshot(
        world.payload,
        world.master,
        snapshot_id=spec.world_id,
        normalization_code_sha="0" * 64,
    )
    verify_manifest(snapshot, static_calendar)  # must not raise despite floor
    assert any(a.kind == "split" for a in snapshot.actions), "splits must still fire early"


def test_total_hazard_bound_rejected() -> None:
    """Round-1 P1-2: a rate set whose per-session event hazard reaches 1.0
    monopolizes the walk and is not a valid world."""
    from tree_options.synth.spec import ActionRates

    with pytest.raises(ValueError, match="total event hazard"):
        base_spec(
            rates=ActionRates(
                split=252.0,
                reverse_split=0.0,
                cash_dividend=0.0,
                stock_dividend=0.0,
                rename=0.0,
                merger=0.0,
                bankruptcy=0.0,
                voluntary_delisting=0.0,
                coverage_lapse=0.0,
                ipo_per_year=0.0,
            )
        )


def test_empty_sectors_rejected() -> None:
    """Round-1 P1-1: sectors=() passes uniqueness but crashes generation."""
    with pytest.raises(ValueError, match="at least one sector"):
        base_spec(sectors=())


def test_session_returns_bounded_under_gate() -> None:
    """Round-1 remediation: undeclared overnight moves are clamped strictly
    inside the 2x discontinuity bound (fat tails cannot emit an ungated
    doubling)."""
    from tree_options.synth.generate import DAILY_RET_LIMIT, _clamp_session_return

    assert _clamp_session_return(5.0) == DAILY_RET_LIMIT
    assert _clamp_session_return(-5.0) == -DAILY_RET_LIMIT
    assert _clamp_session_return(0.01) == 0.01
    assert DAILY_RET_LIMIT < math.log(2.0), "clamp must stay under the gate bound"


def test_features_panel_passes_availability_audit(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """Packet criterion 5: a features_as_of panel over a generated world is
    compliant under the AvailabilityGuard's own decision instants."""
    from tree_options.data.authority import PointInTimeDataset
    from tree_options.guards.availability import AvailabilityGuard

    spec = base_spec()
    world = generate_world(spec, static_calendar)
    snapshot = ingest_snapshot(
        world.payload,
        world.master,
        snapshot_id=spec.world_id,
        normalization_code_sha="0" * 64,
    )
    ds = PointInTimeDataset(snapshot, static_calendar, universe_id="SYNTH-U")
    guard = AvailabilityGuard(static_calendar)
    mid = static_calendar.sessions()[100]
    decision = guard.decision_instant(mid)
    panel = ds.features_as_of(
        decision_at=decision, universe_id="SYNTH-U", dataset_snapshot_id=spec.world_id
    )
    result = guard.audit_panel(panel)
    assert result.compliant, "panel must have compliant rows"
    assert result.n_rejected == 0, f"availability rejections: {result.rejections[:3]}"


def test_tampered_world_fails_quality_gate(static_calendar) -> None:  # type: ignore[no-untyped-def]
    spec = base_spec()
    world = generate_world(spec, static_calendar)
    dup = world.payload.bars[0].model_dump()
    rows = [bar.model_dump() for bar in world.payload.bars]
    rows.append(dup)
    rows += [action.model_dump() for action in world.payload.actions]
    from tree_options.data.raw import build_payload

    payload = build_payload(
        provider="synthetic/v1",
        rows=tuple(rows),
        retrieved_at=world.payload.retrieved_at,
    )
    snapshot = ingest_snapshot(
        payload,
        world.master,
        snapshot_id=spec.world_id + "-tampered",
        normalization_code_sha="0" * 64,
    )
    with pytest.raises(DataQualityError, match="duplicate bar"):
        verify_manifest(snapshot, static_calendar)
