"""Workstream B: synthetic world generator v1 (M2 packet §3.B).

The generator is a vendor: it emits RawPayload rows through the unchanged
M1 ingest → manifest → authority pipeline. Determinism is the contract
(same spec ⇒ byte-identical payload ⇒ identical content_sha256), the
truth sidecar must be unreachable outside synth/, and every generated
world must be quality-gate clean.
"""

from __future__ import annotations

import ast
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
    # same seed: identical seats/sectors/tickers, different closes
    assert [s.security_id for s in alpha_world.master] == [s.security_id for s in null_world.master]
    assert alpha_world.payload.bars != null_world.payload.bars, "planted effect must move closes"


def test_truth_sidecar_import_boundary() -> None:
    """synth.truth must be unreachable outside tree_options.synth.* — the
    planted-effect parameters can never leak into feature construction."""
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
                if target == "tree_options.synth.truth" or target.startswith(
                    "tree_options.synth.truth."
                ):
                    offenders.append(f"{module} imports {target}")
    assert not offenders, f"truth sidecar leaked into: {offenders}"


def test_every_security_carries_sector(static_calendar) -> None:  # type: ignore[no-untyped-def]
    world = generate_world(base_spec(), static_calendar)
    assert world.master
    for record in world.master:
        assert isinstance(record, SecurityMasterRecord)
        assert record.sector_mappings, f"{record.security_id} has no sector"
        assert record.sector_on(record.listing_start, as_of=_pub(record.listing_start))


def test_lifecycle_scenarios_present(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """All nine M1 fixture scenarios must be reachable with boosted rates."""
    world = generate_world(base_spec(n_securities=40), static_calendar)
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
    sessions = static_calendar.sessions()
    later = [r for r in world.master if r.listing_start > sessions[0]]
    assert later, "IPO scenario: later-cohort listings must exist"
    assert world.truth.recycled_tickers, "ticker recycle scenario must occur"


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
