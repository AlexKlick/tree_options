// RL-3 pure helpers for the Outlook tab — no React, no fetch. Mirrors
// lib/scenario.ts as the precedent: formatting/derivation logic lives
// here so the component stays thin and the honesty rules (coverage
// always with n, degraded metrics never rendered as numbers, no fan
// without a receipt) are testable without mounting anything.

import type {
  ForecastCoverage90,
  ForecastFanEntry,
  ForecastHorizonStatus,
  ForecastMetadata,
  ForecastModelMetrics,
  ForecastResultWire,
  ForecastRunResultResponse,
} from './types'

/** A completed result is either a receipt or a typed refusal record —
 * both are completed runs; only the receipt may render a fan. */
export function isForecastRefusal(
  wire: ForecastResultWire | { refusal: string | null } | null | undefined,
): boolean {
  return (
    wire !== null &&
    wire !== undefined &&
    typeof wire === 'object' &&
    'refusal' in wire &&
    typeof (wire as { refusal: unknown }).refusal === 'string'
  )
}

/** Metrics withheld with a reason (aggregate overflow) — never rendered
 * as numbers; the reason line is the display. */
export function isDegradedMetrics(
  metrics: object,
): metrics is { aggregate_status: 'non_finite'; reason: string; n_evaluated: number } {
  return (metrics as { aggregate_status?: unknown }).aggregate_status === 'non_finite'
}

/** Percent with a fixed digit count; null-safe (no NaN ever renders). */
export function fmtPct(v: number | null | undefined, dp = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return `${(100 * v).toFixed(dp)}%`
}

/** Numeric level formatting (index levels, widths, scores) — never USD:
 * this surface carries statistics, not money. */
export function fmtLevel(v: number | null | undefined, dp = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return v.toFixed(dp)
}

function fmtRange(lo: number | null, hi: number | null, dp = 2): string {
  if (lo === null || hi === null) return '—'
  return `[${fmtLevel(lo, dp)}, ${fmtLevel(hi, dp)}]`
}

/** The honesty rule as a string: coverage is ALWAYS quoted with its n.
 * "12/19 (63.2%) · Wilson 95% [0.42, 0.81] · bootstrap [0.38, 0.84]".
 * Degenerate bootstrap (null bounds + reason) renders the reason. */
export function coverageLine(cov: ForecastCoverage90): string {
  const parts = [`${cov.hits}/${cov.n}`]
  if (cov.point !== null) parts.push(`(${fmtPct(cov.point)})`)
  parts.push(`Wilson 95% ${fmtRange(cov.wilson_low, cov.wilson_high, 3)}`)
  if (cov.bootstrap_low !== null && cov.bootstrap_high !== null) {
    parts.push(`bootstrap ${fmtRange(cov.bootstrap_low, cov.bootstrap_high, 3)}`)
  } else if (cov.bootstrap_reason) {
    parts.push(`bootstrap: ${cov.bootstrap_reason}`)
  }
  return parts.join(' · ')
}

/** Ordered fan levels aligned to the declared tau grid. The wire keys
 * quantiles by two-decimal strings ('0.05'..'0.95'); the grid is the
 * ordering authority, never key insertion order. Missing key or an
 * 'unavailable' entry yields null — the chart draws a gap, never an
 * invented level. */
export function fanLevels(
  entry: ForecastFanEntry,
  grid: number[],
): Array<number | null> {
  if (entry.status !== 'ok' || !entry.quantiles) {
    return grid.map(() => null)
  }
  return grid.map((t) => {
    const v = entry.quantiles?.[t.toFixed(2)]
    return v === undefined || !Number.isFinite(v) ? null : v
  })
}

/** The registry status entry for one (source, horizon) — undefined when
 * the combination is not listed at all. */
export function horizonEntry(
  metadata: ForecastMetadata | null,
  source: string,
  horizon: number,
): ForecastHorizonStatus | undefined {
  return metadata?.sources
    .find((s) => s.source === source)
    ?.horizons.find((h) => h.horizon === horizon)
}

/** True only when the response is a COMPLETED run carrying a receipt
 * (not a refusal): the fan precondition. A queued/running/failed run
 * and a typed refusal all return false — no receipt, no fan. */
export function hasReceipt(
  response: ForecastRunResultResponse | null,
): response is ForecastRunResultResponse & { result: ForecastResultWire } {
  if (!response || response.status !== 'completed' || !response.result) {
    return false
  }
  return !isForecastRefusal(response.result)
}

/** Skill cell text: the baseline row reads "baseline"; a null skill
 * shows its reason instead of a number. */
export function skillLine(
  metrics: ForecastModelMetrics,
): string {
  const skill = metrics.skill_vs_baseline
  if (skill === null) return 'baseline'
  if (skill.pinball_skill === null) return skill.reason ?? 'unavailable'
  const dm = skill.dm
  const dmTxt = dm
    ? ` · DM stat ${dm.stat.toFixed(2)} (p ${dm.p_one_sided.toFixed(3)}, lag ${dm.lag} ${dm.lag_units}, ${dm.direction})`
    : ` · DM unavailable: ${skill.dm_unavailable_reason ?? 'no variance'}`
  return `skill ${fmtPct(skill.pinball_skill)} on ${skill.paired_n} paired origins${dmTxt}`
}
