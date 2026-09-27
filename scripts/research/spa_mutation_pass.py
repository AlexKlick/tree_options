#!/usr/bin/env python3
"""Scoped SPA mutation pass (RL-3b checkpoint C) — honesty gates only.

Mirror of ``forecast_mutation_pass.py`` for the TypeScript half of the
Outlook tab: a fixed registry of hand-named mutants, each breaking ONE
honesty gate (horizon gating, no-fan-without-receipt, exact disabled
copy, coverage-always-with-n, run-keying, the calibrate-copy ban,
refusal-never-renders-as-receipt), applied to a DISPOSABLE COPY of the
tree and killed by the two scoped vitest files. The worktree is never
touched: every mutant starts from pristine bytes, the harness verifies
the anchor appears EXACTLY ONCE before replacing, and restore is
byte-verified against the pristine copy.

KILLED = the scoped vitest run fails. SURVIVED = it passes (a hole in
the oracle net). Any HARNESS_ERROR (baseline fails, anchor drift, copy
failure) aborts with the reason — it never counts as KILLED.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: The whole tree is copied (repo-root config like desk-universe.toml
#: loads at import time even for a web-only pass — the RL-3a trap);
#: only caches, environments, and git metadata are excluded.
#: web/node_modules is symlinked (same dependency versions; deps are
#: not under test).
_COPY_IGNORE = (
    ".venv", "__pycache__", ".git", "*.pyc", ".pytest_cache",
    "artifacts", "dist", "node_modules",
)

_VITEST_FILES = [
    "src/lib/forecast.test.ts",
    "src/components/ResearchOutlook.test.tsx",
]

#: (id, gate, [(file-in-web, [(anchor, replacement), ...]), ...]) —
#: each anchor must appear EXACTLY ONCE in the pristine file. Most
#: mutants are a single edit; M-C3b and M-C6b break redundant lock
#: PAIRS at once (hasReceipt + the inner schema gate; the effect-level
#: drop + the render-level run_id check): either lock alone surviving
#: is by design — the combination must die.

# edit pairs, named so the combined mutants read without paren-counting
_SCHEMA_GATE = (
    "if (!w || w.refusal !== null || w.schema !== 'research-forecast-result/1') {",
    "if (!w) {",
)
_HASRECEIPT_GUARD = (
    "return !isForecastRefusal(response.result)",
    "return true",
)
_RECEIPT_READY_GUARD = (
    "const receiptReady = hasReceipt(result) && result.run_id === runId",
    "const receiptReady = hasReceipt(result)",
)
_EFFECT_SUPERSEDE = (
    "void getForecastResult(runId).then((envelope) => {\n"
    "      if (superseded || resultFetchedFor.current !== runId) return\n"
    "      setResult(envelope)",
    "void getForecastResult(runId).then((envelope) => {\n"
    "      setResult(envelope)",
)

_OUTLOOK = "src/components/ResearchOutlook.tsx"
_HELPERS = "src/lib/forecast.ts"


def _one(rel: str, *edits: tuple[str, str]) -> tuple[str, tuple[tuple[str, str], ...]]:
    return rel, edits


_RegistryEntry = tuple[str, str, tuple[tuple[str, tuple[tuple[str, str], ...]], ...]]
_REGISTRY: tuple[_RegistryEntry, ...] = (
    ("M-C1", "horizon gating (disabled option becomes selectable)",
     (_one(_OUTLOOK,
           ("<option key={h.horizon} value={h.horizon} disabled>",
            "<option key={h.horizon} value={h.horizon}>")),)),
    ("M-C2", "no-fan-without-receipt (hasReceipt admits a refusal)",
     (_one(_HELPERS, _HASRECEIPT_GUARD),)),
    ("M-C3", "refusal renders as a receipt (inner schema gate dropped; redundant with hasReceipt)",
     (_one(_OUTLOOK, _SCHEMA_GATE),)),
    ("M-C3b", "BOTH refusal locks dropped (the combination must die)",
     (_one(_OUTLOOK, _SCHEMA_GATE), _one(_HELPERS, _HASRECEIPT_GUARD))),
    ("M-C4", "coverage without its n (hits/n becomes bare percent)",
     (_one(_HELPERS,
           ("const parts = [`${cov.hits}/${cov.n}`]",
            "const parts = [cov.point !== null ? fmtPct(cov.point) : '']")),)),
    ("M-C5", "exact disabled status_copy dropped from the option text",
     (_one(_OUTLOOK,
           ("{h.horizon} — {h.status_copy}",
            "{h.horizon}")),)),
    ("M-C6", "run-keying outer lock dropped (redundant with the effect drop)",
     (_one(_OUTLOOK, _RECEIPT_READY_GUARD),)),
    ("M-C6b", "BOTH run-keying locks dropped (the combination must die)",
     (_one(_OUTLOOK, _RECEIPT_READY_GUARD, _EFFECT_SUPERSEDE),)),
    ("M-C7", "empty-state copy says calibrate (banned word)",
     (_one(_OUTLOOK,
           ("No evaluation receipt for this horizon yet — run evaluation to",
            "No evaluation receipt for this horizon yet — run to calibrate and")),)),
)


def _run_vitest(web_dir: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env["HOME"] = "/home/alexk"          # npx/vitest need a real HOME
    env["MEM0_TELEMETRY"] = "false"
    proc = subprocess.run(
        ["npx", "vitest", "run", *_VITEST_FILES],
        cwd=web_dir, env=env, capture_output=True, text=True,
        timeout=600,
    )
    return proc.returncode, (proc.stdout + proc.stderr)[-2000:]


def _apply(path: Path, edits: tuple[tuple[str, str], ...]) -> None:
    text = path.read_text()
    for anchor, replacement in edits:
        n = text.count(anchor)
        if n != 1:
            raise RuntimeError(
                f"anchor appears {n}x (must be exactly 1): {anchor[:60]!r}")
        text = text.replace(anchor, replacement)
    path.write_text(text)


#: single-lock survivors that are justified IFF their combined mutant
#: (both locks broken) is KILLED — redundancy by design, not an oracle
#: hole.
_COMBOS: dict[str, str] = {"M-C3": "M-C3b", "M-C6": "M-C6b"}


def main() -> int:
    results: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="rl3b-spa-mut-") as tmp:
        copy = Path(tmp) / "repo"
        shutil.copytree(
            REPO, copy,
            ignore=shutil.ignore_patterns(*_COPY_IGNORE))
        node_modules = REPO / "web" / "node_modules"
        if node_modules.is_dir():
            os.symlink(node_modules.resolve(),
                       copy / "web" / "node_modules")

        web = copy / "web"
        rels = {rel for _id, _g, files in _REGISTRY for rel, _e in files}
        targets = {rel: web / rel for rel in rels}
        pristine = {rel: t.read_bytes() for rel, t in targets.items()
                    if t.exists()}
        missing = [rel for rel in rels if not targets[rel].exists()]
        if missing:
            print(f"HARNESS_ERROR: mutant target(s) missing: {missing}")
            return 2

        rc, tail = _run_vitest(web)
        if rc != 0:
            print("HARNESS_ERROR: pristine baseline fails the scoped "
                  f"vitest run (rc={rc}):\n{tail}")
            return 2
        print("baseline: scoped vitest green on the disposable copy")

        def _restore(files):
            for rel, _edits in files:
                targets[rel].write_bytes(pristine[rel])
                if targets[rel].read_bytes() != pristine[rel]:
                    raise RuntimeError(f"restore failed for {rel}")

        for mid, gate, files in _REGISTRY:
            for rel, _edits in files:
                targets[rel].write_bytes(pristine[rel])   # pristine start
            try:
                for rel, edits in files:
                    _apply(targets[rel], edits)
            except RuntimeError as exc:
                results.append({"id": mid, "gate": gate,
                                "verdict": "HARNESS_ERROR", "detail": str(exc)})
                _restore(files)
                continue
            try:
                rc, tail = _run_vitest(web)
            except subprocess.TimeoutExpired as exc:
                # An infrastructure failure (stall, kill, crash) is
                # NEVER mutation credit: only a vitest run that FAILED
                # (rc == 1, a real assertion/count failure) kills a
                # mutant; rc == 0 survives; anything else is a harness
                # error carrying the tail for diagnosis.
                results.append({"id": mid, "gate": gate,
                                "verdict": "HARNESS_ERROR",
                                "detail": f"vitest timeout: {exc}"})
                _restore(files)
                continue
            if rc == 0:
                verdict, detail = "SURVIVED", "scoped tests still pass"
            elif rc == 1:
                verdict = "KILLED"
                detail = f"rc=1; {tail[-240:]!r}"
            else:
                results.append({"id": mid, "gate": gate,
                                "verdict": "HARNESS_ERROR",
                                "detail": f"unexpected vitest rc={rc}; {tail[-240:]!r}"})
                _restore(files)
                continue
            results.append({"id": mid, "gate": gate, "verdict": verdict,
                            "detail": detail})
            try:
                _restore(files)
            except RuntimeError as exc:
                print(f"HARNESS_ERROR: {exc}")
                return 2

    print(json.dumps(results, indent=2))
    by_id = {r["id"]: r for r in results}
    survived = [
        r for r in results
        if r["verdict"] == "SURVIVED"
        and not (r["id"] in _COMBOS
                 and by_id.get(_COMBOS[r["id"]], {}).get("verdict") == "KILLED")
    ]
    hard_errors = [r for r in results if r["verdict"] == "HARNESS_ERROR"]
    killed = sum(1 for r in results if r["verdict"] == "KILLED")
    justified = sum(
        1 for r in results
        if r["verdict"] == "SURVIVED" and r not in survived)
    print(f"mutation pass: {killed} KILLED / {len(survived)} SURVIVED "
          f"/ {justified} justified-survivor / {len(results)} total")
    return 1 if (survived or hard_errors) else 0


if __name__ == "__main__":
    sys.exit(main())
