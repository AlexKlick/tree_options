#!/usr/bin/env bash
# Gate for cockpit_flash_review.sh: z.ai is a SHARED account. Wait (never
# probing while the Study Forge GDP scheduler runs - its bench excludes
# rate-limited samples) until the scheduler is gone AND one glm-5.3-flash
# probe succeeds, then run the review. Gives up after 24h.
set -uo pipefail
OUT="${1:-/tmp/flash-review}"
mkdir -p "$OUT"
DEADLINE=$(( $(date +%s) + 24 * 3600 ))
probe() {
  python3 - <<'PY'
import json, os, sys, urllib.request, urllib.error
req = urllib.request.Request(
    os.environ.get("ZAI_OPENAI_BASE_URL", "https://api.z.ai/api/coding/paas/v4") + "/chat/completions",
    data=json.dumps({"model": "glm-5.3-flash", "max_tokens": 5,
                     "messages": [{"role": "user", "content": "ok"}]}).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + os.environ.get("ZAI_CODING_API_KEY", "")})
try:
    urllib.request.urlopen(req, timeout=20); sys.exit(0)
except urllib.error.HTTPError as e:
    print(f"probe HTTP {e.code}"); sys.exit(1)
except Exception as e:
    print(f"probe {type(e).__name__}"); sys.exit(1)
PY
}
while :; do
  if (( $(date +%s) > DEADLINE )); then echo "$(date -Is) gave up (24h)" | tee "$OUT/.gave-up"; exit 3; fi
  if pgrep -f gdp_sched.py > /dev/null; then
    echo "$(date -Is) waiting: Study Forge GDP scheduler still running"; sleep 600; continue
  fi
  if probe; then echo "$(date -Is) z.ai free - starting review"; break; fi
  echo "$(date -Is) waiting: z.ai probe failed"; sleep 600
done
exec "$(dirname "$0")/cockpit_flash_review.sh" "$OUT"
