#!/usr/bin/env bash
# Local-only release gate. Deliberately never installs or syncs dependencies,
# changes an editable pointer, creates a hosted workflow, or starts a service.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${TREX_GATE_PYTHON:-$ROOT/.venv/bin/python}"
[[ -x "$PYTHON" ]] || { echo "Missing release interpreter: install the isolated locked environment first." >&2; exit 2; }
export PYTHONPATH="$ROOT/src" UV_NO_SYNC=1 PYTHONDONTWRITEBYTECODE=1
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
"$PYTHON" - <<'PY'
import importlib.util
import importlib.metadata
import tomllib
import pathlib
import sys
import tree_options
actual = pathlib.Path(tree_options.__file__).resolve().parent
expected = pathlib.Path('src/tree_options').resolve()
assert actual == expected, f'import root mismatch: {actual} != {expected}'
requested = tuple(int(v) for v in pathlib.Path('.python-version').read_text().strip().split('.'))
assert sys.version_info[:len(requested)] == requested, 'interpreter differs from .python-version'
missing = [m for m in ('pytest', 'hypothesis', 'ruff', 'mypy', 'fastapi', 'httpx')
           if importlib.util.find_spec(m) is None]
assert not missing, f'gate dependencies missing: {missing}'
critical = ('numpy', 'pydantic', 'pyyaml', 'pytest', 'hypothesis', 'ruff', 'mypy', 'fastapi', 'httpx', 'anyio', 'starlette')
lock = tomllib.loads(pathlib.Path('uv.lock').read_text())
for name in critical:
    locked = {p['version'] for p in lock['package'] if p['name'] == name}
    installed = importlib.metadata.version(name)
    assert installed in locked, f'{name}: {installed} not in locked versions {locked}'
print(f'Gate interpreter {sys.version.split()[0]}; import root {actual}')
PY
"$PYTHON" -m ruff check src tests
"$PYTHON" -m mypy
"$PYTHON" -m pytest -o addopts='' -q
# No | tail: the true failing process exit code is the gate result.
