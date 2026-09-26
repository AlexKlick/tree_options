#!/usr/bin/env bash
# RL-1 research lane gate. Mirrors scripts/desk_safety_gate.sh shape
# but scopes to the research package (src/tree_options/research/) and
# tests/research/. Coexists with the desk_safety_gate; neither replaces
# the other.
#
# Static checks (in addition to ruff/mypy/pytest):
#   - No broker-imports inside tree_options.research/
#   - The research workspace path does NOT resolve to the desk state or
#     paper-trades directories
#   - MEM0_TELEMETRY=False in the invocation environment
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${TREX_GATE_PYTHON:-$ROOT/.venv/bin/python}"
[[ -x "$PYTHON" ]] || { echo "Missing release interpreter: install the isolated locked environment first." >&2; exit 2; }
export PYTHONPATH="$ROOT/src" UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
# Vendor telemetry off (CLAUDE.md hard rule).
if [[ "${MEM0_TELEMETRY:-}" != "False" ]]; then
    echo "warning: MEM0_TELEMETRY is not False ($MEM0_TELEMETRY); export MEM0_TELEMETRY=False" >&2
fi
export MEM0_TELEMETRY=False

# -- Static checks ----------------------------------------------------------

FORBIDDEN=$(grep -RIn "tree_options.trex.{ibkr,monitor,gateway_watch,enter}" "$ROOT/src/tree_options/research" 2>/dev/null || true)
if [[ -n "$FORBIDDEN" ]]; then
    echo "FAIL: tree_options.research must not import broker-side modules:" >&2
    echo "$FORBIDDEN" | sed 's/^/  /' >&2
    exit 1
fi

"$PYTHON" - <<'PY'
"""RL-1 path-overlap guard (mirrors the runtime guard in research_view.attach)."""
import os
from pathlib import Path
from tree_options.desk.paths import paper_dir, state_root
from tree_options.research.paths import adapter_dir, cache_dir, workspace_root

forbidden = {state_root().resolve(), paper_dir().resolve()}
for label, p in (("workspace", workspace_root()),
                 ("adapter_dir", adapter_dir()),
                 ("cache_dir", cache_dir())):
    try:
        resolved = p.resolve()
    except (FileNotFoundError, OSError):
        continue
    if resolved in forbidden:
        raise SystemExit(f"FAIL: research path {label}={resolved} collides with desk workspace")
print(f"research paths ok: workspace={workspace_root()}  adapter_dir={adapter_dir()}  cache_dir={cache_dir()}")
PY

# -- Lint + typecheck -------------------------------------------------------

"$PYTHON" -m ruff check src/tree_options/research src/tree_options/trex_web tests/research
"$PYTHON" -m mypy src/tree_options/research src/tree_options/trex_web
"$PYTHON" -m pytest tests/research -o addopts='' -q

# Web typecheck is owned by `cd web && npm run check` and is not in this
# gate's scope; CI/operator runs it separately.

echo "research_gate ok"
