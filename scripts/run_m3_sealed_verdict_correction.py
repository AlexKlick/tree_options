#!/usr/bin/env python3
"""Recompute the sealed M3 options gate verdict from the IMMUTABLE stamped
artifacts with the corrected criterion 4 (owner ruling C, 2026-08-20 —
docs/m3-sealed-gate-criterion4-decision.md).

The one-shot gate ran and recorded FAIL (exit 4) solely because its
criterion-4 check compared every open position's contract expiration
against the WORLD's last session, when the pre-declared criterion means
"no position open past the evaluated end of the fold that holds it" —
the trials run per-fold with a clamped window, and an arm-B position
near a fold's end legitimately stays open when its contract expires
after that fold's evaluated window. Root cause, the wrong-bound
analysis, and the disposition are recorded in the memo above; the
original FAIL stays verbatim in /tmp/m3-sealed-gate.log and
sealed-gate-summary.json.

This script re-evaluates ALL SEVEN criteria against the SAME payload
files in artifacts/m3-options-sealed/ (no trials re-run, no second
trial registry) and stamps the corrected verdict alongside them as
sealed-gate-corrected-verdict.json. The fold geometry needed by the
corrected criterion is re-derived exactly as the gate derived it
(frozen registry world -> bar sessions -> protocol walk-forward splits,
verified against the stamped n_folds), and every open position must map
to an owning fold — a position that cannot be mapped is a failure, not
a skip.

Exit 0 = corrected verdict PASS; 4 = FAIL; anything else = error.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EQUITY_REGISTRY = REPO_ROOT / "data" / "worlds" / "registry.json"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / "artifacts" / "m3-options-sealed"

NULLS = ("synth-v1-val-null-701", "synth-v1-val-null-702")
POWERS = ("synth-v1-val-alpha-710", "synth-v1-val-alpha-711")
SEALED_WORLDS = (*NULLS, *POWERS)
ARMS = ("A", "B")
END_BUFFER_SESSIONS = 6  # trials/options_run.py: the fold-window clamp

FIDELITY_FLOOR = 0.696
CRITICAL_T = 1.96
FP_T_BOUND = 2.5
REJECTION_FLOOR = 100
VOLUME_TAIL_FRACTION = 0.05
ZERO_BID_CODES = ("ZeroSizeQuoteError", "NonpositiveQuoteError", "NO_VISIBLE_QUOTE")
FIXED_CLOCK = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


def _t_from_series(series: list[float]) -> float | None:
    n = len(series)
    if n < 3:
        return None
    sd = statistics.stdev(series)
    if sd <= 0:
        return None
    return (sum(series) / n) * (n**0.5) / sd


def _fold_geometry(world_id: str, calendar, protocol) -> list[tuple[date, date, date]]:
    """(first_test, last_test, evaluated_end) per fold, derived exactly as
    the gate derived its folds: registry world -> bar sessions -> protocol
    walk-forward splits -> the _fold_backtest end clamp."""
    from tree_options.data.ingest import ingest_snapshot
    from tree_options.splitting.splitter import WalkForwardSplitter
    from tree_options.synth import WorldSpec, generate_world

    equity = json.loads(EQUITY_REGISTRY.read_text(encoding="utf-8"))
    entry = next(w for w in equity["worlds"] if w["world_id"] == world_id)
    spec = WorldSpec(**entry["spec"])
    world = generate_world(spec, calendar)
    snap = ingest_snapshot(
        world.payload,
        world.master,
        snapshot_id=world_id,
        normalization_code_sha=equity["generator_code_sha"],
    )
    if snap.manifest.content_sha256 != entry["expected"]["content_sha256"]:
        raise RuntimeError(f"regenerated world {world_id} drifted from the registry pin")
    sessions = tuple(sorted({bar.session for bar in snap.bars}))
    f = protocol.folds
    splitter = WalkForwardSplitter(
        calendar,
        label_horizon_sessions=f.label_horizon_sessions,
        embargo_sessions=f.embargo_sessions,
        val_sessions=f.validation_window_sessions.default,
        test_sessions=f.test_window_sessions.default,
        roll_sessions=f.roll_forward_sessions,
        min_train_sessions=f.min_train_sessions,
    )
    world_set = frozenset(sessions)
    folds = [fl for fl in splitter.splits(sessions) if fl.test_sessions <= world_set]
    geometry = []
    for fl in folds:
        last_execution = calendar.nth_after(max(fl.test_sessions), 1)
        buffered = calendar.nth_after(last_execution, END_BUFFER_SESSIONS)
        end = (
            buffered
            if calendar.ordinal(buffered) <= calendar.ordinal(sessions[-1])
            else sessions[-1]
        )
        geometry.append((min(fl.test_sessions), max(fl.test_sessions), end))
    return geometry


def _owning_fold_end(
    geometry: list[tuple[date, date, date]], calendar, entry_session: date
) -> date | None:
    for first_test, last_test, end in geometry:
        if first_test <= entry_session <= calendar.nth_after(last_test, 1):
            return end
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACTS_DIR)
    args = parser.parse_args(argv)

    from tree_options.protocol.loader import load_protocol
    from tree_options.protocol.stamping import build_stamp, write_artifact
    from tree_options.time.calendar import StaticSessionCalendar

    calendar = StaticSessionCalendar(
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.json",
        REPO_ROOT / "data" / "calendar" / "nyse_sessions_2018_01_02_2026_12_31.sha256",
    )
    protocol = load_protocol(REPO_ROOT / "research_protocol.yaml")

    payloads: dict[tuple[str, str], dict] = {}
    stamp_audit: dict[str, str] = {}
    for world_id, arm in [*((w, a) for w in SEALED_WORLDS for a in ARMS)]:
        path = args.artifacts_dir / f"m3-{world_id}-{arm.lower()}-r1.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        payloads[(world_id, arm)] = body["payload"]
        stamp_audit[f"{world_id}|{arm}"] = body["stamp"]["git_sha"][:8]
    original = json.loads(
        (args.artifacts_dir / "sealed-gate-summary.json").read_text(encoding="utf-8")
    )["payload"]
    if original["verdict"] != "FAIL":
        raise RuntimeError("this correction applies to the recorded FAIL run")

    geometry_by_world: dict[str, list[tuple[date, date, date]]] = {}
    for world_id in SEALED_WORLDS:
        geometry = _fold_geometry(world_id, calendar, protocol)
        stamped_folds = payloads[(world_id, "A")]["n_folds"]
        if len(geometry) != stamped_folds:
            raise RuntimeError(
                f"{world_id}: derived {len(geometry)} folds vs stamped {stamped_folds}"
            )
        geometry_by_world[world_id] = geometry
        print(
            f"CORRECTION_GEOMETRY={world_id} FOLDS={len(geometry)} "
            f"FIRST_END={geometry[0][2]} LAST_END={geometry[-1][2]}"
        )

    failures: list[str] = []
    reported: dict[str, object] = {"per_trial": {}, "power": {}, "fidelity": {}}

    for world_id, arm in [(w, a) for w in SEALED_WORLDS for a in ARMS]:
        payload = payloads[(world_id, arm)]
        counters = payload["counters"]
        per_fold = payload["per_fold"]
        fills = payload["fills_log"]
        key = f"{world_id}|{arm}"

        # 1. conservation every session (trial COMPLETED is the registry's record)
        sessions_evaluated = sum(f["n_sessions_evaluated"] for f in per_fold)
        if sessions_evaluated <= 0 or counters["conservation_checks"] != sessions_evaluated:
            failures.append(
                f"{key}: conservation {counters['conservation_checks']} != "
                f"{sessions_evaluated} evaluated sessions"
            )

        # 2. fill discipline (T+1 execution, quote received by execution)
        if not fills:
            failures.append(f"{key}: no stamped fills")
        for fill in fills:
            if not fill["decision_session"] < fill["execution_session"]:
                failures.append(f"{key}: same-session fill {fill['fill_id']}")
                break
        for fill in fills:
            if fill["quote_received_at"] > fill["execution_at"]:
                failures.append(f"{key}: quote received after execution {fill['fill_id']}")
                break

        # 3. rejection paths live (the owner-ruled amended form)
        rejections = counters["rejections"]
        zero_bid = sum(
            rejections.get(bucket, {}).get(code, 0)
            for bucket in (
                "entry_fill_rejections",
                "exit_fill_rejections",
                "force_close_rejections",
            )
            for code in ZERO_BID_CODES
        )
        hist = counters["rule_histogram"]
        rule_evals = sum(sum(s.values()) for s in hist.values())
        volume_fails = hist.get("same_day_volume", {}).get("FAIL", 0)
        if zero_bid < REJECTION_FLOOR:
            failures.append(f"{key}: zero-bid rejections {zero_bid} < {REJECTION_FLOOR}")
        if rule_evals == 0 or volume_fails / rule_evals < VOLUME_TAIL_FRACTION:
            failures.append(
                f"{key}: volume tail {volume_fails}/{rule_evals} below "
                f"{VOLUME_TAIL_FRACTION:.0%}"
            )

        # 4. machinery terminal states (arm B) — CORRECTED BOUND: the owning
        #    fold's evaluated end, not the world's last session
        open_past_fold_end = 0
        unmapped = 0
        if arm == "B":
            geometry = geometry_by_world[world_id]
            for position in payload["pooled"]["positions"]:
                if position["exit_kind"] is not None:
                    continue
                end = _owning_fold_end(
                    geometry, calendar, date.fromisoformat(position["entry_session"])
                )
                if end is None:
                    unmapped += 1
                    continue
                if date.fromisoformat(position["contract_expiration"]) <= end:
                    open_past_fold_end += 1
                    if open_past_fold_end <= 3:
                        failures.append(
                            f"{key}: position {position['underlying_security_id']} open past "
                            f"its fold's evaluated end {end} (expiry "
                            f"{position['contract_expiration']})"
                        )
            if unmapped:
                failures.append(f"{key}: {unmapped} open positions map to no fold")
            print(
                f"CORRECTION_CRIT4={key} OPEN="
                f"{sum(1 for p in payload['pooled']['positions'] if p['exit_kind'] is None)} "
                f"UNMAPPED={unmapped} PAST_FOLD_END={open_past_fold_end}"
            )

        reported["per_trial"][key] = {  # type: ignore[index]
            "sessions_evaluated": sessions_evaluated,
            "conservation_checks": counters["conservation_checks"],
            "zero_bid_rejections": zero_bid,
            "volume_fail_fraction": (volume_fails / rule_evals) if rule_evals else None,
            "n_positions": payload["pooled"]["n_positions"],
            "fidelity_rho": payload["pooled"]["fidelity_rho"],
        }

    # 5. vehicle fidelity on the nulls (arm A)
    null_rhos = [payloads[(w, "A")]["pooled"]["fidelity_rho"] for w in NULLS]
    if any(rho is None for rho in null_rhos):
        failures.append(f"fidelity rho unmeasurable on a null world: {null_rhos}")
    else:
        mean_rho = sum(null_rhos) / len(null_rhos)  # type: ignore[arg-type]
        reported["fidelity"] = {"null_rhos_arm_a": null_rhos, "mean_rho": mean_rho}  # type: ignore[index]
        if mean_rho < FIDELITY_FLOOR:
            failures.append(f"vehicle fidelity {mean_rho:.4f} < floor {FIDELITY_FLOOR}")
    reported["fidelity"]["null_rhos_arm_b"] = [  # type: ignore[index]
        payloads[(w, "B")]["pooled"]["fidelity_rho"] for w in NULLS
    ]

    # 6. FP bound on each null world (arm A)
    for world_id in NULLS:
        t = _t_from_series(payloads[(world_id, "A")]["pooled"]["stride4_cohort_ics"])
        if t is None:
            failures.append(f"stride-4 t unmeasurable on {world_id}")
            continue
        reported["power"][world_id] = t  # type: ignore[index]
        if abs(t) > FP_T_BOUND:
            failures.append(f"FP null {world_id}: |t|={t:.3f} > {FP_T_BOUND}")

    # 7. power transfer, pooled over 710+711 (arm A)
    pooled_ics: list[float] = []
    per_world_ts = {}
    for world_id in POWERS:
        series = payloads[(world_id, "A")]["pooled"]["stride4_cohort_ics"]
        per_world_ts[world_id] = _t_from_series(series)
        pooled_ics.extend(series)
    pooled_t = _t_from_series(pooled_ics)
    reported["power"]["pooled_t"] = pooled_t  # type: ignore[index]
    reported["power"]["per_world_t"] = per_world_ts  # type: ignore[index]
    reported["power"]["n_pooled_cohorts"] = len(pooled_ics)  # type: ignore[index]
    if pooled_t is None or pooled_t < CRITICAL_T:
        failures.append(f"power transfer pooled t={pooled_t} < {CRITICAL_T}")

    verdict = "PASS" if not failures else "FAIL"
    summary = {
        "gate": "m3-options-sealed/1-corrected",
        "correction_of": "m3-options-sealed/1 (recorded FAIL)",
        "ruling": "owner option C, 2026-08-20 (docs/m3-sealed-gate-criterion4-decision.md)",
        "what_changed": (
            "criterion 4's bound: owning fold's evaluated end, not the world's "
            "last session; criteria 1/2/3/5/6/7 recomputed identically"
        ),
        "ran_at": FIXED_CLOCK.isoformat(),
        "stamp_audit": stamp_audit,
        "original_failures": original["failures"],
        "failures": failures,
        "reported": reported,
        "verdict": verdict,
    }
    stamp = build_stamp(
        protocol,
        trial_id="m3-options-sealed-corrected-verdict",
        config={"gate": "m3-options-sealed/1-corrected", "correction": "criterion-4 bound"},
        dataset_manifest_hash=sha256(
            b"".join(
                (args.artifacts_dir / f"m3-{w}-{a.lower()}-r1.json").read_bytes()
                for w in SEALED_WORLDS
                for a in ARMS
            )
        ).hexdigest(),
        repo=REPO_ROOT,
    )
    write_artifact(args.artifacts_dir / "sealed-gate-corrected-verdict.json", summary, stamp)
    print(f"CORRECTED_VERDICT={verdict}")
    for failure in failures:
        print(f"CORRECTED_CHECK FAIL {failure}")
    return 0 if verdict == "PASS" else 4


if __name__ == "__main__":
    sys.exit(main())
