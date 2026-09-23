#!/usr/bin/env bash
# Long-dated option-bar capture for the options desk (plan Step 0 item 8):
# daily bars for ATM +/-2 strikes, calls and puts, on the monthly expiries
# that actually traded (third Friday, or the session before a closed one),
# 90-270 calendar DTE, for the 35 optionable panel names, at 27 as_of dates
# (about every 4 weeks, 2024-09-27 .. 2026-09-22).
#
# RESUMABLE: re-run this script. The content-addressed cache makes every page
# already captured free, so progress is durable; only uncaptured pages spend
# wire requests. Sizing, cadence rationale and the resume recipe:
# docs/desk-runbook.md, section "Long-dated option capture".
#
# Writes ONLY artifacts/massive-cache-desk (cache) and
# artifacts/desk-longdated-capture (captures); artifacts/massive-cache is read
# (stage 0 copies master pages OUT of it) and never written.
set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACTS="${DESK_CAPTURE_ARTIFACTS:-/home/alexk/documents/tree_options/artifacts}"
PY="${DESK_CAPTURE_PYTHON:-/home/alexk/documents/tree_options/.venv/bin/python}"
CACHE="$ARTIFACTS/massive-cache-desk"
OUT="$ARTIFACTS/desk-longdated-capture"
SEED_FROM="$ARTIFACTS/massive-cache"
export PYTHONPATH="$REPO/src"

case "$CACHE" in */massive-cache) echo "refusing the load-bearing cache" >&2; exit 64;; esac

NAMES="$("$PY" -c 'from tree_options.desk.universe import CHAIN_UNIVERSE; print(",".join(CHAIN_UNIVERSE))')" \
  || { echo "cannot resolve the desk universe" >&2; exit 65; }
IMPORTED="$("$PY" -c 'import tree_options; print(tree_options.__file__)')"
case "$IMPORTED" in "$REPO"/*) ;; *) echo "WRONG TREE: $IMPORTED" >&2; exit 66;; esac

# Every 4 weeks from the first Friday inside the free tier's rolling 2-year
# window; the two holiday Fridays (2025-07-04, 2026-07-03) move to the
# Thursday session before; the last is the latest complete session at launch.
AS_OFS=(
  2024-09-27 2024-10-25 2024-11-22 2024-12-20 2025-01-17 2025-02-14 2025-03-14
  2025-04-11 2025-05-09 2025-06-06 2025-07-03 2025-08-01 2025-08-29 2025-09-26
  2025-10-24 2025-11-21 2025-12-19 2026-01-16 2026-02-13 2026-03-13 2026-04-10
  2026-05-08 2026-06-05 2026-07-02 2026-07-31 2026-08-28 2026-09-22
)
AS_OF_FLAGS=()
for d in "${AS_OFS[@]}"; do AS_OF_FLAGS+=(--as-of "$d"); done

COMMON=(
  --underlyings "$NAMES" --pages-per-master 25 --max-pages 25 --timeout 30
  --bars-mode atm-grid --bars-strike-band 2 --bars-expiries monthly-traded --bars-sides both
  --dte-min 90 --dte-max 270 --cache-dir "$CACHE" --out-dir "$OUT"
)
capture() { "$PY" "$REPO/scripts/capture_massive_structural.py" "${COMMON[@]}" "$@"; }

echo "== code $REPO ($IMPORTED) names=$NAMES"
echo "== stage 0: seed masters from the coverage-era cache (read-only) $(date -Is)"
"$PY" "$REPO/scripts/seed_massive_cache.py" --from-cache "$SEED_FROM" --to-cache "$CACHE" \
  --underlyings "$NAMES" "${AS_OF_FLAGS[@]}"
rc0=$?

echo "== stage 1: masters + spot for every as_of; --bars 0 logs the exact series count $(date -Is)"
capture "${AS_OF_FLAGS[@]}" --bars 0 --budget 4000
rc1=$?

echo "== stage 2: the oldest as_of's bars first (its first bars sit nearest the 2-year edge) $(date -Is)"
capture --as-of "${AS_OFS[0]}" --bars 5000 --budget 6000
rc2=$?

echo "== stage 3: everything (stage 1-2 pages are cache hits) $(date -Is)"
capture "${AS_OF_FLAGS[@]}" --bars 40000 --budget 44000
rc3=$?

echo "== done $(date -Is) seed=$rc0 sizing=$rc1 oldest=$rc2 full=$rc3"
exit "$rc3"
