#!/usr/bin/env bash
# M0 exact-head gate (audit §7). Non-mutating, fail-closed, no overrides in
# the recorded promotion run. Dev-only dirty-tree escape: M0_GATE_ALLOW_DIRTY=1.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ "${M0_GATE_ALLOW_DIRTY:-0}" != "1" ]; then
  if ! git diff-index --quiet HEAD -- || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    echo "REFUSED: dirty worktree (commit or stash first)." >&2
    exit 2
  fi
fi

echo "== head =="; git rev-parse HEAD
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
  --json artifacts/m0-mutations.json \
  --markdown artifacts/m0-mutations.md
echo "== uv build =="
uv build
echo "== wheel smoke (fresh environment) =="
SMOKE=$(mktemp -d)
trap 'rm -rf "$SMOKE"' EXIT
uv venv "$SMOKE/venv" -q
uv pip install --python "$SMOKE/venv/bin/python" -q dist/tree_options-*.whl
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
echo "== gate complete =="
