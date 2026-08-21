#!/usr/bin/env bash
# M3 final gate (gate3): verbatim scripts/m0_gate.sh at e21e20a, only the
# mutation-output filenames renamed (m0-mutations -> m3-gate3-mutations).
# the recorded promotion run. Dev-only dirty-tree escape: M0_GATE_ALLOW_DIRTY=1.
set -euo pipefail

# numpy-era determinism: BLAS thread counts change reduction orders
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
       NUMEXPR_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1

cd /home/alexk/documents/tree_options

if [ "${M0_GATE_ALLOW_DIRTY:-0}" != "1" ]; then
  if ! git diff-index --quiet HEAD -- || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    echo "REFUSED: dirty worktree (commit or stash first)." >&2
    exit 2
  fi
fi

echo "== head =="; HEAD_AT_START=$(git rev-parse HEAD); echo "$HEAD_AT_START"
# Exact-head + clean-tree assertions run from an EXIT trap so they hold on
# EVERY exit path (mid-run failure included), not only the success tail.
SMOKE=""
_gate_exit_check() {
  local rc=$?
  if [ -n "${SMOKE:-}" ]; then rm -rf "$SMOKE"; fi
  local head_at_end
  head_at_end=$(git rev-parse HEAD 2>/dev/null || echo unknown)
  if [ "$head_at_end" != "$HEAD_AT_START" ]; then
    echo "REFUSED: head moved during the gate ($HEAD_AT_START -> $head_at_end)." >&2
    exit 3
  fi
  local unexpected
  unexpected=$(git status --porcelain | grep -v -E '^[ ?]{2}(artifacts|dist)/' | grep -v -E '^\?\? (artifacts|dist)/' || true)
  if [ -n "$unexpected" ]; then
    echo "REFUSED: working tree changed beyond generated artifacts:" >&2
    echo "$unexpected" >&2
    exit 4
  fi
  return "$rc"
}
trap _gate_exit_check EXIT
echo "== uv sync --frozen (dev group included) =="
uv sync --frozen
echo "== ruff format --check =="
uv run --frozen ruff format --check src tests scripts
echo "== ruff check =="
uv run --frozen ruff check src tests scripts
echo "== mypy =="
uv run --frozen mypy
echo "== compileall =="
uv run --frozen python -m compileall -q src tests scripts
echo "== pytest -W error (hypothesis profile: default, derandomized, 50 examples) =="
uv run --frozen pytest -W error
echo "== mutation harness =="
uv run --frozen python scripts/mutate.py \
  --json artifacts/m3-gate3-mutations.json \
  --markdown artifacts/m3-gate3-mutations.md
echo "== uv build =="
uv build
echo "== wheel smoke (fresh environment) =="
SMOKE=$(mktemp -d)  # cleaned by the EXIT trap
uv venv "$SMOKE/venv" -q
uv pip install --python "$SMOKE/venv/bin/python" -q dist/tree_options-*.whl
# The wheel carries code; the frozen protocol yaml is repo data (single
# source of truth, never duplicated into the wheel) — point the loader at it.
TREE_OPTIONS_PROTOCOL="$PWD/research_protocol.yaml" \
"$SMOKE/venv/bin/python" - <<'PY'
from decimal import Decimal
import tree_options
from tree_options.protocol.loader import load_protocol, protocol_hash
from tree_options.guards.fills import fraction_to_midpoint
p = load_protocol()
assert len(p.invariants) == 14
assert protocol_hash(p)
assert fraction_to_midpoint(Decimal("1.00"), Decimal("1.10"), "buy", Decimal("1")) == Decimal("1.05")
print(f"wheel smoke ok: tree_options {tree_options.__version__ if hasattr(tree_options, '__version__') else ''} protocol {p.meta.protocol_version}")
PY
echo "== exact-head + clean-tree assertions (enforced by the EXIT trap) =="
echo "head unchanged: $(git rev-parse HEAD); tree clean beyond artifacts/ and dist/"
echo "== gate complete =="
