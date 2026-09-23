#!/usr/bin/env bash
# M8 flash review: glm-5.3-flash (via claude-zai, print mode) drives the
# Playwright MCP through every cockpit route x viewport and reports
# findings. Each lane runs TWICE; only findings reported by both runs are
# promoted (flash output = leads, not facts - Codex re-verifies after).
#
# Usage: scripts/cockpit_flash_review.sh [outdir]   (default /tmp/flash-review)
# Sequential on purpose: one z.ai session at a time (shared account; the
# Study Forge bench excludes samples that hit rate limits).
set -uo pipefail

OUT="${1:-/tmp/flash-review}"
BASE="${TREX_COCKPIT_URL:-http://127.0.0.1:8090/}"
PLAN_ID="${TREX_REVIEW_PLAN:-$(curl -s "${BASE}api/plans" | python3 -c 'import json,sys; p=json.load(sys.stdin)["plans"]; print(p[-1]["id"] if p else "")')}"
SYMBOL="${TREX_REVIEW_SYMBOL:-SPY}"
mkdir -p "$OUT"

declare -A LANES=(
  [plans]="#/"
  [discover]="#/discover"
  [performance]="#/stats"
  [market]="#/market"
  [symbol]="#/market/${SYMBOL}"
  [plan]="#/plan/${PLAN_ID}"
)

prompt_for() {
  local route="$1"
  cat <<EOF
You are a meticulous UI reviewer using ONLY the Playwright browser tools. Target: ${BASE}${route}
(a read-only options-trading cockpit; hash routing; dark theme).

For EACH viewport in this order: 390x844, 1600x900, 2360x984:
1. browser_resize to the viewport, browser_navigate to the target URL, then browser_navigate to it
   AGAIN (a hash-only change does not reload), wait 2 seconds.
2. browser_take_screenshot (scale css) and browser_snapshot.
3. browser_evaluate: () => ({ overflow: document.documentElement.scrollWidth - innerWidth,
   small: [...document.querySelectorAll('button,a,input')].filter(e => { const b = e.getBoundingClientRect();
   return b.width > 0 && b.height < 40 && innerWidth < 500 }).length })
4. browser_console_messages level "error" and browser_network_requests (static false).
Check: console errors; failed or non-2xx requests; any request URL that is not under ${BASE};
horizontal overflow > 0; text clipped, overlapping, or unreadable in the screenshot; buttons or links
without an accessible name in the snapshot; numbers without units or context; delayed/stale data
shown without an age indicator; anything that would confuse an operator.

RULES: never click, type, or submit anything (no Run scan, watch, unwatch, approve, dismiss, Ask for
ideas, Refresh data, scenario). Only navigate, resize, observe. Do not guess - report only what a tool
result or the screenshot shows.

Reply with ONLY a markdown list, one finding per line, exactly:
- [blocker|major|minor] <viewport> | <element or selector> | <what is wrong> | <evidence: exact text/metric>
and for a clean viewport: - none at <viewport>
EOF
}

for lane in plans discover performance market symbol plan; do
  for run in 1 2; do
    dest="$OUT/${lane}-r${run}.md"
    [[ -s "$dest" ]] && { echo "skip $dest (exists)"; continue; }
    echo "=== $(date -Is) lane=$lane run=$run"
    ZAI_MODEL=glm-5.3-flash timeout 900 /home/alexk/.local/bin/claude-zai -p "$(prompt_for "${LANES[$lane]}")" \
      --allowedTools "mcp__plugin_playwright_playwright" \
      > "$dest.tmp" 2> "$OUT/${lane}-r${run}.err"
    rc=$?
    if [[ $rc -eq 0 && -s "$dest.tmp" ]]; then mv "$dest.tmp" "$dest"; else
      echo "lane=$lane run=$run FAILED rc=$rc (see ${lane}-r${run}.err)"; fi
  done
done
echo "=== $(date -Is) done" | tee "$OUT/.done"
