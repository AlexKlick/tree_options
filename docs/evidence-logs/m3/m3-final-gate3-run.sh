#!/usr/bin/env bash
# M3 gate3 wrapper: run the final full gate at the docs-only final head X,
# full log capture, GATE3_EXIT echo, done marker. NO commits may land while
# this runs (gate2's EXIT=3 lesson: the exact-head trap is live).
set -uo pipefail
export HOME=/home/alexk
cd /home/alexk/documents/tree_options
{
  echo "GATE3_HEAD=$(git rev-parse HEAD) LAUNCHED=$(date -Is) NOTE=final-docs-only-head-after-evidence-finalize INNER=verbatim-m0_gate.sh-renamed-outputs"
  bash /tmp/m3-gate3-inner.sh
  echo "GATE3_INNER_EXIT=$?"
} > /tmp/m3-final-gate3.log 2>&1
echo "GATE3_WRAPPER_DONE=$(date -Is)" >> /tmp/m3-final-gate3.log
touch /tmp/m3-final-gate3.done
