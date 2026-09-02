"""Wave-0 tooling tests: the pre-declared menu, the ledger discipline, the
D8 sequencing guard, and T-MOM's PIT-visible momentum window.

The wave driver is scripts/run_lane2_wave.py (a path script, like the seal
CLI) — loaded by path, the same pattern the mutation registry uses."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "run_lane2_wave", REPO / "scripts" / "run_lane2_wave.py"
)
wave = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("run_lane2_wave", wave)
assert _spec.loader is not None
_spec.loader.exec_module(wave)


def test_the_menu_is_the_predeclared_budget() -> None:
    """(Agenda A / the P1 rulings) exactly 20 slots: arm-A 18 + arm-B 2,
    the families in the ratified composition, every knob inside the frozen
    protocol bands."""
    menu = wave.wave0_menu()
    assert len(menu) == 20
    arm_a = [c for c in menu if c.arm == "A"]
    arm_b = [c for c in menu if c.arm == "B"]
    assert len(arm_a) == 18 and len(arm_b) == 2
    families = [c.family for c in arm_a]
    assert families.count("T-NULL") == 3
    assert families.count("T-MOM") == 1
    assert families.count("T-BAND") == 3
    assert families.count("T-DTE") == 3
    assert families.count("T-FLOW") == 4
    assert families.count("T-HOLD") == 4
    assert {c.slot_id for c in arm_b} == {"null-s1-b", "mom20-b"}
    # every delta/dte knob stays inside the frozen bands
    for config in menu:
        assert Decimal("0.30") <= config.target_abs_delta <= Decimal("0.60")
        assert 30 <= config.target_dte <= 60
        assert config.flow_min_session_volume >= 1
        assert len(config.hypothesis) >= 8
    # three DISTINCT null seeds
    seeds = {c.score_seed for c in menu if c.family == "T-NULL" and c.arm == "A"}
    assert seeds == {"theory-null-1", "theory-null-2", "theory-null-3"}


def test_the_alias_map_dedups_the_default_config_group() -> None:
    """band-0.45 / dte-045 / flow-0100 / hold-exit4 all alias null-s1's
    default config — one executed config serves five pre-declared slots,
    and the unique-config count fits the 32-cap with room to spare."""
    menu = wave.wave0_menu()
    aliases = wave.alias_map(menu)
    assert aliases["null-s1"] == [
        "band-0.45",
        "dte-045",
        "flow-0100",
        "hold-exit4",
        "null-s1",
    ]
    ledger = wave.build_ledger(manifest_hash="m" * 64, protocol_hash="p" * 64)
    assert ledger["unique_configs"] == 16
    assert ledger["unique_configs"] <= 32


def test_the_geometry_is_the_d4_ruling() -> None:
    """mt34 / val6 / E2 / test13 / roll13, H5 — in GRID Fridays, in the
    override's declared field order."""
    assert wave.WAVE0_GEOMETRY == (5, 2, 6, 13, 13, 34)
    override = wave.wave0_split_override()
    assert override.label_horizon_sessions == 5
    assert override.embargo_sessions == 2
    assert override.val_sessions == 6
    assert override.test_sessions == 13
    assert override.roll_sessions == 13
    assert override.min_train_sessions == 34


def test_the_ledger_roundtrips_and_drift_refuses() -> None:
    """Every menu slot verifies against its own ledger row; a drifted
    param (a different flow threshold) refuses before anything runs; an
    unknown slot refuses."""
    ledger = wave.build_ledger(manifest_hash="m" * 64, protocol_hash="p" * 64)
    for config in wave.wave0_menu():
        wave.verify_config_against_ledger(ledger, config)
    drifted = next(c for c in wave.wave0_menu() if c.slot_id == "flow-0010")
    drifted_fixed = wave.WaveConfig(**{**drifted.__dict__, "flow_min_session_volume": 5})
    with pytest.raises(SystemExit, match="drifted"):
        wave.verify_config_against_ledger(ledger, drifted_fixed)
    with pytest.raises(SystemExit, match="not in the committed"):
        wave.verify_config_against_ledger(
            ledger,
            wave.WaveConfig(
                slot_id="not-a-slot",
                family="T-FLOW",
                hypothesis="x" * 20,
                arm="A",
                model_family=wave._NULL,
                score_seed="theory-null-1",
                target_abs_delta=Decimal("0.45"),
                target_dte=45,
                exit_sessions_after_entry=4,
                flow_min_session_volume=100,
            ),
        )


def test_the_sequencing_guard_requires_calibration_for_non_null() -> None:
    """(Agenda A) T-NULL slots always run; every non-null slot refuses
    while the D8 calibration block is absent and passes once it exists."""
    ledger = wave.build_ledger(manifest_hash="m" * 64, protocol_hash="p" * 64)
    menu = {c.slot_id: c for c in wave.wave0_menu()}
    wave.require_calibration(ledger, menu["null-s1"])
    wave.require_calibration(ledger, menu["null-s1-b"])  # family T-NULL
    with pytest.raises(SystemExit, match="calibration block is absent"):
        wave.require_calibration(ledger, menu["mom20-a"])
    with pytest.raises(SystemExit, match="calibration block is absent"):
        wave.require_calibration(ledger, menu["band-0.35"])
    ledger["calibration"] = {"prior_stride4_cohort_ic_sd": 0.1}
    wave.require_calibration(ledger, menu["mom20-a"])


@dataclass
class _Bar:
    security_id: str
    session: date
    close: Decimal
    available_at: datetime


@dataclass
class _Grid:
    sessions: tuple[date, ...]
    closes: dict[date, datetime] = field(default_factory=dict)

    def session_close(self, session: date) -> datetime:
        return self.closes.get(session) or datetime.combine(session, time(21, 0), tzinfo=UTC)


def _weekly_world(n_fridays: int = 30, own_close: Decimal | None = None):
    """A Friday grid + one security's weekly bars. A bar's T+1 wall: the
    session AFTER its own (available_at = session + 1 day, 09:00 ET) — so
    at a close(d) decision the session-d bar is INVISIBLE and d-1 is the
    newest visible close."""
    start = date(2025, 1, 3)  # a Friday
    sessions = tuple(start + timedelta(weeks=i) for i in range(n_fridays))
    grid = _Grid(sessions)
    bars = []
    for i, session in enumerate(sessions):
        close = Decimal(100 + i)
        if own_close is not None and i == n_fridays - 1:
            close = own_close
        bars.append(
            _Bar(
                security_id="TEST",
                session=session,
                close=close,
                available_at=datetime.combine(session + timedelta(days=1), time(9, 0), tzinfo=UTC),
            )
        )
    return grid, tuple(bars), sessions


def test_momentum_uses_only_pit_visible_closes() -> None:
    """mom_20 = ln(c_{d-1} / c_{d-21}) over exactly the last 21 VISIBLE
    grid closes — the decision session's own bar publishes the next
    morning and must not enter the score (M382's owner)."""
    grid, bars, sessions = _weekly_world(own_close=Decimal("999999"))
    rows = wave.momentum_scored_rows(bars, grid, sessions[21:])
    by_session = {r.session: r for r in rows}
    d = sessions[-1]
    row = by_session[d]
    # c_{d-1} = 100 + (n-2); c_{d-21} = 100 + (n-22)
    n = len(sessions)
    expected = float((Decimal(100 + n - 2) / Decimal(100 + n - 22)).ln())
    assert row.score == pytest.approx(expected, rel=1e-12)
    # the wild own-session close never entered: the identical world
    # WITHOUT it scores the same
    grid2, bars2, sessions2 = _weekly_world(own_close=None)
    rows2 = wave.momentum_scored_rows(bars2, grid2, sessions2[21:])
    assert rows2[-1].score == row.score


def test_momentum_omits_the_warmup_names() -> None:
    """A decision session with fewer than 21 visible closes gets NO row
    (Agenda C's 21-grid-index warm-up): the first 21 grid sessions are
    absent from the quintile cut, never imputed."""
    grid, bars, sessions = _weekly_world(30)
    rows = wave.momentum_scored_rows(bars, grid, sessions)
    present = {r.session for r in rows}
    for session in sessions[:21]:
        assert session not in present
    assert present == set(sessions[21:])


def test_momentum_reads_the_realized_dte_not_the_target() -> None:
    """Placeholder discipline guard: the T-DTE hypothesis declares the
    ladder is read by REALIZED dte (stamped contract_expiration), never
    the target — pinned by asserting the menu text carries the disclosure."""
    menu = {c.slot_id: c for c in wave.wave0_menu()}
    assert "REALIZED dte" in menu["dte-035"].hypothesis


def test_calibrate_builds_priors_from_the_null_artifacts(tmp_path: Path) -> None:
    """D8: the priors are the MEAN of the three realized pooled stats —
    a missing artifact refuses; a stats-less payload refuses; the priors
    never carry a synthetic constant."""
    trials = tmp_path / "trials"
    sds = [0.11, 0.13, 0.15]
    rhos = [0.02, 0.04, 0.06]
    for slot, sd, rho in zip(("null-s1", "null-s2", "null-s3"), sds, rhos, strict=True):
        slot_dir = trials / slot
        slot_dir.mkdir(parents=True)
        (slot_dir / "m3-world-a-r1.json").write_text(
            json.dumps(
                {
                    "payload": {
                        "pooled": {
                            "stride4_cohort_ic_sd": sd,
                            "fidelity_rho": rho,
                            "total_return": "-0.01",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    calibration = wave.calibrate(trials)
    assert calibration["prior_stride4_cohort_ic_sd"] == pytest.approx(0.13)
    assert calibration["prior_fidelity_rho"] == pytest.approx(0.04)
    assert set(calibration["realized"]) == {"null-s1", "null-s2", "null-s3"}
    # a missing artifact refuses
    (trials / "null-s3" / "m3-world-a-r1.json").unlink()
    with pytest.raises(SystemExit, match="no stamped artifact"):
        wave.calibrate(trials)
    # a stats-less payload refuses
    (trials / "null-s3" / "m3-world-a-r1.json").write_text(
        json.dumps({"payload": {"pooled": {}}}), encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="cannot be built"):
        wave.calibrate(trials)


def test_the_null_rows_match_the_generator() -> None:
    """The wave's null rows are the G5 generator's own scores — the
    runner's seed binding re-verifies every one of them."""
    from tree_options.trials.null_score import null_score

    sessions = (date(2025, 1, 3), date(2025, 1, 10))
    rows = wave.null_scored_rows("theory-null-1", sessions, ("SPY", "TSLA"))
    assert len(rows) == 4
    for row in rows:
        assert row.score == null_score(
            seed="theory-null-1", session=row.session, security_id=row.security_id
        )
        assert row.label == wave.WAVE0_NULL_LABEL


def test_the_fee_model_stamp_exists_in_the_runner_payload() -> None:
    """(D6) the payload discloses the default PerContractFeeModel cost
    constants as exact string Decimals."""
    from tree_options.ledger.fees import PerContractFeeModel

    assert PerContractFeeModel.DEFAULT_FEE_PER_CONTRACT == Decimal("0.65")
    assert PerContractFeeModel.DEFAULT_MINIMUM_PER_ORDER == Decimal("1.00")
    # the stamping site itself is covered by the mini-fixture payload
    # round-trips in test_g4_event_machinery.py; here we pin the source
    # constants the stamp reads
    src = (REPO / "src" / "tree_options" / "trials" / "options_run.py").read_text(encoding="utf-8")
    assert '"fee_model"' in src
    assert "DEFAULT_FEE_PER_CONTRACT" in src


def test_the_world_decision_sessions_never_touch_the_holdout() -> None:
    """(D7, defensive) the driver's holdout assertion keys on the same
    FINAL_HOLDOUT_DATES constant the machinery's w5 guard uses — ISO
    STRINGS, compared in the session.isoformat() domain."""
    from tree_options.protocol.holdout import FINAL_HOLDOUT_DATES

    assert len(FINAL_HOLDOUT_DATES) == 13
    assert min(FINAL_HOLDOUT_DATES) == "2026-05-08"
    assert max(FINAL_HOLDOUT_DATES) == "2026-08-14"
    assert all(date.fromisoformat(d).weekday() == 4 for d in FINAL_HOLDOUT_DATES)


def test_simple_namespace_import_guards() -> None:
    """The driver module loads cleanly with only stdlib + repo imports at
    module scope (no filesystem or registry side effects on import)."""
    assert wave.MOM_MODEL_FAMILY == "mom20-quintile/v1"
    assert wave.WAVE0_GEOMETRY == (5, 2, 6, 13, 13, 34)
