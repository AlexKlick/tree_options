// Freshness of a watchdog verdict (GatewayBanner, ExitMachineBanner). The
// timers write every 60s and the server flags a file older than 300s, but
// a verdict cached in the page ages too: a poll that hangs must not keep an
// old "ok" on screen (Codex 2026-09-23).
export const WATCH_STALE_S = 300

export function watchAge(
  v: { checked_at: number | null; age_seconds: number | null },
  nowS: number,
): number | null {
  if (v.checked_at === null) return v.age_seconds
  return Math.max(v.age_seconds ?? 0, nowS - v.checked_at)
}
