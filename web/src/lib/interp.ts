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
  const i = nearestIndex(
    points.map((p) => p[0]),
    x,
  )
  const [x0, y0] = points[i]
  const [x1, y1] = points[Math.min(i + 1, last)]
  if (x1 === x0) return y0
  return y0 + ((y1 - y0) * (x - x0)) / (x1 - x0)
}
