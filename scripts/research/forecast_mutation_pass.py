#!/usr/bin/env python3
"""Scoped mutation pass for the RL-3 forecast lane (checkpoint B).

Follows ``scripts/mutate.py``'s discipline — disposable copy, anchor
must appear EXACTLY once, baseline selectors must pass before each
mutant, byte-verified restore — with a registry scoped to
``research/forecast`` + the worker's forecast path. Acceptance: every
mutant KILLED (owning selectors fail behaviorally).

Usage (from the rl3-forecast worktree root):
    .venv/bin/python scripts/research/forecast_mutation_pass.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = REPO / ".venv" / "bin" / "python"

MUTANTS = [
    dict(
        id="M-RL3-01-floor-boundary",
        file="src/tree_options/research/forecast/engine.py",
        anchor="below = [r for r in runs if r.n_evaluated < origin_floor]",
        replacement=("below = [r for r in runs if r.n_evaluated <= origin_floor]"),
        selectors=["tests/research/test_forecast_engine.py"],
        invariant="the origin floor is >= 12: exactly-12 publishes",
    ),
    dict(
        id="M-RL3-02-tally-drop",
        file="src/tree_options/research/forecast/harness.py",
        anchor="failures[failed] = failures.get(failed, 0) + 1",
        replacement="failures[failed] = failures.get(failed, 0)",
        selectors=["tests/research/test_forecast_harness.py"],
        invariant="failed origins are counted, never hidden",
    ),
    dict(
        id="M-RL3-03-target-off-by-one",
        file="src/tree_options/research/forecast/harness.py",
        anchor="        target = sessions[t + horizon]",
        replacement="        target = sessions[t + horizon + 1]",
        selectors=["tests/research/test_forecast_harness.py"],
        invariant="the target is the h-th row after the origin row",
    ),
    dict(
        id="M-RL3-04-order-check-gutted",
        file="src/tree_options/research/forecast/harness.py",
        anchor="elif any(b < a for a, b in itertools.pairwise(vals)):",
        replacement="elif False:",
        selectors=["tests/research/test_forecast_harness.py"],
        invariant="unordered quantiles fail, never repair",
    ),
    dict(
        id="M-RL3-05-provenance-filter-dropped",
        file="src/tree_options/research/forecast/sources.py",
        anchor='if entry.get("source") == name and \\',
        replacement='if entry.get("status") in _SUCCESS_STATUSES and \\',
        selectors=["tests/research/test_forecast_sources.py"],
        invariant="provenance binds THIS source, not the last line",
    ),
    dict(
        id="M-RL3-06-bench-zero-ratio",
        file="src/tree_options/research/forecast/metrics.py",
        anchor="if bench <= 0.0:",
        replacement="if bench < 0.0:",
        selectors=["tests/research/test_forecast_metrics.py"],
        invariant="zero baseline loss refuses a skill ratio",
    ),
    dict(
        id="M-RL3-07-scaled-one-step-bands",
        file="src/tree_options/research/forecast/harness.py",
        anchor=(
            "errors = np.asarray(\n"
            "        [float(logs[u + h]) - point_h(u)\n"
            "         for u in range(last - h + 1)], dtype=np.float64)"
        ),
        replacement=(
            "errors = np.asarray(\n"
            "        [float(logs[u + 1])\n"
            "         - (beta0 + phi * float(logs[u]))\n"
            "         for u in range(last)], dtype=np.float64)"
        ),
        selectors=["tests/research/test_forecast_models.py"],
        invariant="ar1 bands are DIRECT h-step errors",
    ),
    dict(
        id="M-RL3-08-intersection-to-union",
        file="src/tree_options/research/forecast/engine.py",
        anchor="set(model_loss_by_date) & set(baseline_loss_by_date)",
        replacement="set(model_loss_by_date) | set(baseline_loss_by_date)",
        selectors=["tests/research/test_forecast_engine.py"],
        invariant="skill/DM cohorts are the matched intersection",
    ),
    dict(
        id="M-RL3-09-dm-lag-inflated",
        file="src/tree_options/research/forecast/metrics.py",
        anchor="return dm_test(list(d), lag=lag)",
        replacement="return dm_test(list(d), lag=lag + 20)",
        selectors=["tests/research/test_forecast_metrics.py"],
        invariant="DM lag is 1 origin unit (hand-pinned stat)",
    ),
    dict(
        id="M-RL3-10-authority-skip",
        file="src/tree_options/research/forecast/sources.py",
        anchor="if iso not in authority:",
        replacement="if False:",
        selectors=["tests/research/test_forecast_sources.py"],
        invariant="non-session vendor rows are excluded + counted",
    ),
    # -- checkpoint B surviving mutations (codex-named, now killed) --
    dict(
        id="M-RL3-11-ledger-pinball-orientation",
        file="src/tree_options/research/forecast/harness.py",
        anchor=("tau * (actual - q) if actual >= q else (1.0 - tau) * (q - actual)"),
        replacement=("(1.0 - tau) * (actual - q) if actual >= q else tau * (q - actual)"),
        selectors=["tests/research/test_forecast_harness.py"],
        invariant="production ledger losses keep the pinball "
        "orientation (tau charges under-forecast)",
    ),
    dict(
        id="M-RL3-12-per-origin-orientation",
        file="src/tree_options/research/forecast/metrics.py",
        anchor=(
            "    err = y[:, None] - q\n"
            "    losses = np.maximum(t * err, (t - 1.0) * err)\n"
            "    return 2.0 * losses.mean(axis=1)"
        ),
        replacement=(
            "    err = y[:, None] - q\n"
            "    losses = np.maximum((1.0 - t) * err, "
            "-t * err)\n"
            "    return 2.0 * losses.mean(axis=1)"
        ),
        selectors=["tests/research/test_forecast_metrics.py"],
        invariant="per-origin grid losses keep the pinball orientation at asymmetric tau",
    ),
    dict(
        id="M-RL3-13-bootstrap-block-1",
        file="src/tree_options/research/forecast/metrics.py",
        anchor="        block_size=block_size,",
        replacement="        block_size=1,",
        selectors=[
            "tests/research/test_forecast_metrics.py",
            "tests/research/test_forecast_engine.py",
        ],
        invariant="the coverage bootstrap uses the declared moving "
        "block (2), not independent resampling",
    ),
    dict(
        id="M-RL3-14-dm-sensitivity-frozen",
        file="src/tree_options/research/forecast/engine.py",
        anchor="                alt = dm_on_differentials(d, lag=lag)",
        replacement=("                alt = dm_on_differentials(d, lag=DM_LAG_ORIGIN_UNITS)"),
        selectors=["tests/research/test_forecast_engine.py"],
        invariant="each DM sensitivity entry re-runs the test at ITS "
        "lag (0/1/2), not the primary lag",
    ),
    dict(
        id="M-RL3-15-floor-first-run-only",
        file="src/tree_options/research/forecast/engine.py",
        anchor="below = [r for r in runs if r.n_evaluated < origin_floor]",
        replacement=("below = [r for r in runs[:1] if r.n_evaluated < origin_floor]"),
        selectors=["tests/research/test_forecast_engine.py"],
        invariant="the origin floor is per MODEL: a non-baseline below "
        "the floor refuses even when the baseline passes",
    ),
]

# Whole-tree copy minus generated outputs (mirrors mutate.py's
# DISPOSABLE_COPY_IGNORE): the import chain reads repo-root config at
# import time — desk.universe loads desk-universe.toml — so a
# hand-picked directory list silently breaks collection.
_COPY_IGNORE = (
    ".venv",
    "__pycache__",
    ".git",
    "*.pyc",
    ".pytest_cache",
    "artifacts",
    "dist",
)


def _run(pytest_args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = {
        "PYTHONPATH": str(cwd / "src"),
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/alexk",
        "MEM0_TELEMETRY": "False",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    return subprocess.run(
        [str(PY), "-m", "pytest", *pytest_args, "-o", "addopts=", "-q"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rl3-mutation-"))
    copy = tmp / "repo"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(*_COPY_IGNORE))

    baselines: dict[tuple[str, ...], bool] = {}
    results = []
    try:
        for m in MUTANTS:
            selectors = tuple(m["selectors"])
            if selectors not in baselines:
                r = _run(list(selectors), copy)
                baselines[selectors] = r.returncode == 0
            if not baselines[selectors]:
                results.append(
                    {
                        "id": m["id"],
                        "verdict": "HARNESS_ERROR",
                        "detail": "baseline selectors failed",
                    }
                )
                continue
            target = copy / m["file"]
            original = target.read_bytes()
            text = original.decode()
            if text.count(m["anchor"]) != 1:
                results.append(
                    {
                        "id": m["id"],
                        "verdict": "MUTATION_DRIFT",
                        "detail": f"anchor count {text.count(m['anchor'])}",
                    }
                )
                continue
            target.write_text(text.replace(m["anchor"], m["replacement"]))
            try:
                r = _run(list(selectors), copy)
            finally:
                target.write_bytes(original)
                restored = hashlib.sha256(target.read_bytes()).hexdigest()
                assert restored == hashlib.sha256(original).hexdigest()
            behavioral = r.returncode != 0 and ("FAILED" in r.stdout or "failed" in r.stdout)
            if behavioral:
                verdict = "KILLED"
            elif r.returncode != 0:
                verdict = "INVALID_MUTANT"
            else:
                verdict = "SURVIVED"
            results.append(
                {
                    "id": m["id"],
                    "verdict": verdict,
                    "invariant": m["invariant"],
                    "selectors": list(selectors),
                }
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    report = {
        "schema": "rl3-mutation-pass/1",
        "mutants": results,
        "killed": sum(1 for r in results if r["verdict"] == "KILLED"),
        "survived": sum(1 for r in results if r["verdict"] == "SURVIVED"),
        "other": sum(1 for r in results if r["verdict"] not in ("KILLED", "SURVIVED")),
    }
    print(json.dumps(report, indent=2))
    return 0 if report["survived"] == 0 and report["other"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
