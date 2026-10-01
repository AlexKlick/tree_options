#!/usr/bin/env bash
# Durable transcript and exit receipt for M0. Call through gate-slot.sh and
# host-test; this wrapper does not change the gate or grant merge authority.
set -euo pipefail
cd "$(dirname "$0")/.."

capture_dir=${1:-artifacts/m0-captured}
mkdir -p "$capture_dir"
capture_dir=$(cd "$capture_dir" && pwd)
capture_head=$(git rev-parse HEAD)
capture_id="$(date -u +%Y%m%dT%H%M%SZ)-${capture_head:0:12}-$$"
capture_log="$capture_dir/$capture_id.log"
capture_receipt="$capture_dir/$capture_id.receipt"

_capture_exit() {
  local rc=$?
  trap - EXIT
  {
    printf 'head=%s\n' "$capture_head"
    printf 'log=%s\n' "$capture_log"
    printf 'exit=%s\n' "$rc"
    printf 'finished_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$capture_receipt"
  exit "$rc"
}
trap _capture_exit EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

printf 'M0 transcript: %s\nM0 receipt: %s\n' "$capture_log" "$capture_receipt"
bash scripts/m0_gate.sh > "$capture_log" 2>&1
