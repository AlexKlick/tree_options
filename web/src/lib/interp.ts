// Nearest-point lookup and linear interpolation over sorted series.
// The server emits kinks and the breakeven exactly, so interpolation
// between adjacent points is exact for the payoff curve.

export const clamp = (v: number, lo: number, hi: number): number =>
  Math.min(hi, Math.max(lo, v))

/** Index of the nearest x in an ascending array (binary search). */
export function nearestIndex(xs: number[], x: number): number {
  if (xs.length === 0) return 0
  if (x <= xs[0]) return 0
  const last = xs.length - 1
  if (x >= xs[last]) return last
  let lo = 0
  let hi = last
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (xs[mid] <= x) lo = mid
    else hi = mid
  }
  return x - xs[lo] <= xs[hi] - x ? lo : hi
}

/** Linear interpolation of y at x over [x, y] pairs (clamped at ends). */
export function interpAt(points: [number, number][], x: number): number {
  if (points.length === 0) return 0
  if (points.length === 1) return points[0][1]
  if (x <= points[0][0]) return points[0][1]
  const last = points.length - 1
  if (x >= points[last][0]) return points[last][1]
  // bracket: points[lo][0] <= x < points[hi][0]
  let lo = 0
  let hi = last
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1
    if (points[mid][0] <= x) lo = mid
    else hi = mid
  }
  const [x0, y0] = points[lo]
  const [x1, y1] = points[hi]
  if (x1 === x0) return y0
  return y0 + ((y1 - y0) * (x - x0)) / (x1 - x0)
}

/** The observation-gap threshold for a series: 30x the median sample
 * spacing, floored at 2 minutes. Cadence-relative on purpose — a 20s-tick
 * monitor series breaks its line across an overnight hold (17h >> 10min)
 * while a daily-bar series never breaks on a weekend (2d << 30d). A line
 * drawn across an unobserved span reads as a move that never happened. */
export function gapBreakMs(points: [number, number][]): number {
  if (points.length < 2) return Number.POSITIVE_INFINITY
  const deltas: number[] = []
  for (let i = 1; i < points.length; i++) {
    const d = points[i][0] - points[i - 1][0]
    if (d > 0) deltas.push(d)
  }
  if (deltas.length === 0) return Number.POSITIVE_INFINITY
  deltas.sort((a, b) => a - b)
  return Math.max(deltas[deltas.length >> 1] * 30, 120_000)
}

/** The series split into contiguous runs with no gap over ``breakMs``
 * (single-point runs included, in order). */
export function splitAtGaps(
  points: [number, number][],
  breakMs: number,
): [number, number][][] {
  if (points.length === 0) return []
  const runs: [number, number][][] = [[points[0]]]
  for (let i = 1; i < points.length; i++) {
    if (points[i][0] - points[i - 1][0] > breakMs) runs.push([])
    runs[runs.length - 1].push(points[i])
  }
  return runs
}

/** A gap-compressed x scale: each run occupies its share of total
 * OBSERVED time and every gap contributes a small fixed allowance
 * (capped at 30% of the axis in total), so a chart that breaks across
 * nights still draws ~all of its width instead of three stubs around
 * 70% void. Null when the series is one run — keep the plain
 * time-proportional mapping there. */
export interface GapScale {
  runs: Array<{ t0: number; t1: number; a: number; b: number }>
}

export function gapScale(
  points: [number, number][],
  breakMs: number,
  gapAllow = 0.03,
): GapScale | null {
  const runs = splitAtGaps(points, breakMs)
  if (runs.length < 2) return null
  const nGaps = runs.length - 1
  const allow = Math.min(gapAllow, 0.3 / nGaps)
  const spans = runs.map((r) => Math.max(r[r.length - 1][0] - r[0][0], 1))
  const total = spans.reduce((a, b) => a + b, 0)
  const content = 1 - allow * nGaps
  const out: GapScale['runs'] = []
  let cursor = 0
  for (let i = 0; i < runs.length; i++) {
    const w = (content * spans[i]) / total
    out.push({ t0: runs[i][0][0], t1: runs[i][runs[i].length - 1][0], a: cursor, b: cursor + w })
    cursor += w + (i < nGaps ? allow : 0)
  }
  return { runs: out }
}

/** Normalized x position [0,1] of time ``t`` under a :GapScale (clamped
 * at the ends; inside a gap allowance it bridges linearly). */
export function scalePos(scale: GapScale, t: number): number {
  const rs = scale.runs
  if (t <= rs[0].t0) return 0
  const last = rs[rs.length - 1]
  if (t >= last.t1) return 1
  for (let i = 0; i < rs.length; i++) {
    const r = rs[i]
    if (t <= r.t1) {
      if (t < r.t0) {
        // between runs: bridge the allowance linearly
        const prev = rs[i - 1]
        const f = (t - prev.t1) / Math.max(r.t0 - prev.t1, 1)
        return prev.b + f * (r.a - prev.b)
      }
      return r.a + ((t - r.t0) / Math.max(r.t1 - r.t0, 1)) * (r.b - r.a)
    }
  }
  return 1
}

/** Inverse of :scalePos: the time at normalized position [0,1]. Inside a
 * gap allowance (no observation there) it snaps to the nearer run edge,
 * so a hover in the void crosshairs the last real sample before it. */
export function scaleTime(scale: GapScale, n: number): number {
  const rs = scale.runs
  if (n <= 0) return rs[0].t0
  if (n >= 1) return rs[rs.length - 1].t1
  for (let i = 0; i < rs.length; i++) {
    const r = rs[i]
    if (n <= r.b) {
      if (n < r.a) {
        const prev = rs[i - 1]
        return n < (prev.b + r.a) / 2 ? prev.t1 : r.t0
      }
      return r.t0 + ((n - r.a) / Math.max(r.b - r.a, Number.EPSILON)) * (r.t1 - r.t0)
    }
  }
  return rs[rs.length - 1].t1
}
