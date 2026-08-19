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


def _synth_import_offenders(module: str, source: str) -> list[str]:
    """All import forms that reach tree_options.synth from `module`:
    absolute, from-root (`from tree_options import synth`), aliased, and
    RELATIVE imports resolved against the importing module's package
    (round-2 P2-1)."""
    offenders: list[str] = []

    def flag(target: str) -> None:
        if target == "tree_options.synth" or target.startswith("tree_options.synth."):
            offenders.append(f"{module} imports {target}")

    mod_parts = module.split(".")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                flag(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            if level == 0:
                base_parts = (node.module or "").split(".") if node.module else []
            else:
                # __init__.py modules carry their package as the last part,
                # which makes this resolution correct for both file kinds
                pkg_parts = mod_parts[: len(mod_parts) - level]
                base_parts = pkg_parts + ((node.module or "").split(".") if node.module else [])
            base = ".".join(base_parts)
            flag(base)
            if base in ("tree_options", "tree_options.synth"):
                for alias in node.names:
                    flag(f"{base}.{alias.name}")
    return offenders


def test_import_lint_catches_all_forms() -> None:
    """Round-2 P2-1: the lint recognizes every import shape that reaches
    synth — asserted against offender snippets, not just the live tree."""
    absolute_forms = [
        "import tree_options.synth",
        "from tree_options import synth",
        "from tree_options.synth import generate_world",
        "from tree_options.synth.truth import WorldTruth",
        "import tree_options.synth.truth",
    ]
    for src in absolute_forms:
        assert _synth_import_offenders("tree_options.data.foo", src), src
    relative_forms = [
        "from ..synth import generate_world",
        "from ..synth.truth import WorldTruth",
        "from .. import synth",
    ]
    for src in relative_forms:
        assert _synth_import_offenders("tree_options.data.__init__", src), src
    assert _synth_import_offenders("tree_options.__init__", "from . import synth")
    # clean imports stay clean
    assert not _synth_import_offenders(
        "tree_options.data.foo", "from tree_options.data import ingest_snapshot"
    )
    # synth-internal imports are exempt at the caller, not the helper
    assert _synth_import_offenders(
        "tree_options.data.foo", "from tree_options.synth.generate import generate_world"
    )


def _is_synth_module(module: str) -> bool:
    """Exact package membership (round-3 P2-2): tree_options.synthesis or
    tree_options.synth_adapter are NOT synth modules and must be scanned."""
    return module == "tree_options.synth" or module.startswith("tree_options.synth.")


def test_synth_module_predicate_is_exact() -> None:
    assert _is_synth_module("tree_options.synth.generate")
    assert _is_synth_module("tree_options.synth.__init__")
    assert not _is_synth_module("tree_options.synthesis")
    assert not _is_synth_module("tree_options.synth_adapter.x")
    assert not _is_synth_module("tree_options.data.synth")


def test_truth_sidecar_import_boundary() -> None:
    """Ground truth is unreachable from feature-construction code: no module
    outside tree_options.synth.* may import synth in ANY form (round-2 P2-1)."""
    src_root = Path(__file__).resolve().parents[2] / "src" / "tree_options"
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        module = ".".join(path.relative_to(path.parents[2]).with_suffix("").parts)
        if _is_synth_module(module):
            continue
        offenders.extend(_synth_import_offenders(module, path.read_text()))
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


# ---------------------------------------------------------------------------
# Round-2 remediation tests


def _hostile_rates() -> ActionRates:
    return ActionRates(
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
    )


def test_minimum_close_floor() -> None:
    """Round-2 P1-1: closes never quantize below $1.00, where cent rounding
    is small enough (<= 0.5%) that no clamped move or bounded bankruptcy can
    land on the 0.5x/2x gate bounds."""
    from decimal import Decimal

    from tree_options.synth.generate import MIN_CLOSE, _cents

    assert MIN_CLOSE >= Decimal("1.00")
    assert _cents(Decimal("0.004")) == MIN_CLOSE
    assert _cents(Decimal("1.00")) == Decimal("1.00")


def test_quantized_moves_stay_inside_gate_property() -> None:
    """Round-2 P1-1, the universal argument (property, not example): for ANY
    close at/above the floor and ANY return (clamped) or bounded bankruptcy
    loss, the cent-quantized next close keeps the pairwise factor strictly
    inside (0.5, 2.0) — so no accepted spec can emit an undeclared move the
    discontinuity gate would reject."""
    import math
    from decimal import Decimal as D

    from tree_options.synth.generate import MIN_CLOSE, _cents, _clamp_session_return

    worst_down = _cents(MIN_CLOSE * D(repr(math.exp(-_clamp_session_return(99.0)))))
    assert MIN_CLOSE / worst_down < D(2)
    worst_bankrupt = _cents(MIN_CLOSE * D("0.51"))
    assert MIN_CLOSE / worst_bankrupt < D(2)
    # dense sweep of the boundary region rather than only the corners
    cents = [D(f"{c:.2f}") for c in [100, 101, 102, 105, 110, 119, 150, 200, 500, 1000, 50000]]
    rets = [-0.6419, -0.5, -0.3, 0.0, 0.3, 0.5, 0.6419]
    for c0 in cents:
        for r in rets:
            c1 = _cents(D(repr(float(c0) / 100 * math.exp(_clamp_session_return(r)))))
            factor = (c0 / 100) / c1
            assert D("0.5") < factor < D(2), (c0, r, factor)
        for loss in ("0.40", "0.445", "0.49"):
            c1 = _cents(c0 / 100 * (D(1) - D(loss)))
            factor = (c0 / 100) / c1
            assert factor < D(2), (c0, loss, factor)


def test_suppression_is_alpha_independent(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """Round-2 P1-2: suppression decisions read the ALPHA-INDEPENDENT base
    price trajectory, so a null world and its same-seed alpha world make
    identical suppression choices — identical event timelines even in a
    hostile floor-hugging spec."""
    null_world = generate_world(base_spec(rates=_hostile_rates()), static_calendar)
    alpha_world = generate_world(
        base_spec(
            kind="alpha",
            # a large coefficient deliberately: the planted drift must be
            # big enough to straddle suppression thresholds, so this test
            # detects a guard that reads the alpha-moved close (M82)
            alpha=AlphaSpec(family="linear_momentum", coefficient=0.05),
            rates=_hostile_rates(),
        ),
        static_calendar,
    )
    assert alpha_world.truth.events == null_world.truth.events
    assert [a.source_record_id for a in alpha_world.payload.actions] == [
        a.source_record_id for a in null_world.payload.actions
    ]
    # the hostile spec really does drive seats to the floor: suppressions
    # must have happened somewhere in the pool
    assert len(null_world.payload.actions) < 200 * 24 * 160 / 252 / 10, "splits must be suppressed"


def _verify_world(world, calendar, snapshot_suffix: str = "") -> None:  # type: ignore[no-untyped-def]
    snapshot = ingest_snapshot(
        world.payload,
        world.master,
        snapshot_id=world.spec.world_id + snapshot_suffix,
        normalization_code_sha="0" * 64,
    )
    verify_manifest(snapshot, calendar)


def test_hostile_specs_verify_across_seeds(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """Round-3 P1-1: the hostile-rate cleanliness property is not a
    one-seed accident — a seed sweep must stay gate-clean, catching the
    announcement/application price gap (a split announced at $2.00 whose
    intervening return lands the applied product under the floor)."""
    for seed in range(1, 13):
        world = generate_world(
            base_spec(world_id=f"synth-v1-hostile-{seed}", seed=seed, rates=_hostile_rates()),
            static_calendar,
        )
        _verify_world(world, static_calendar)


def test_hostile_alpha_world_verifies(static_calendar) -> None:  # type: ignore[no-untyped-def]
    """Round-3 P1-1: the ALPHA trajectory must honor declared ratios too —
    cumulative planted drift cannot push an override under the floor."""
    world = generate_world(
        base_spec(
            kind="alpha",
            alpha=AlphaSpec(family="linear_momentum", coefficient=0.05),
            rates=_hostile_rates(),
        ),
        static_calendar,
    )
    _verify_world(world, static_calendar)
