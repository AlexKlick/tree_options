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
