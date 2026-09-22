// Pos/neg area polygons for the payoff line, split at the zero line.
// Pure + unit-testable: takes the data-space series and a pixel scale.

export interface PixelScale {
  sx: (price: number) => number
  sy: (pnl: number) => number
  zeroY: number
}

export interface Region {
  sign: 'pos' | 'neg'
  polygon: string
}

interface Run {
  sign: 'pos' | 'neg'
  pts: [number, number][]
}

/**
 * Splits the payoff polyline into fills above (pos) and below (neg) the
 * zero baseline. The server emits every breakeven exactly, so cutting on
 * sign changes lands each split on the crossing.
 */
export function payoffRegions(
  points: [number, number][],
  s: PixelScale,
): Region[] {
  if (points.length < 2) return []
  const runs: Run[] = []
  let cur: [number, number][] = [points[0]]
  for (let i = 1; i < points.length; i++) {
    const p = points[i]
    cur.push(p)
    const prev = points[i - 1]
    const crossed = prev[1] > 0 !== p[1] > 0
    if (crossed && cur.length > 1) {
      runs.push({ sign: signOf(cur), pts: cur })
      cur = [p]
    }
  }
  if (cur.length > 1) runs.push({ sign: signOf(cur), pts: cur })

  return runs
    .filter((r) => r.pts.some(([, y]) => Math.abs(y) > 1e-9))
    .map((r) => {
      const px = r.pts.map(([x, y]) => `${s.sx(x).toFixed(1)},${s.sy(y).toFixed(1)}`)
      const first = r.pts[0][0]
      const last = r.pts[r.pts.length - 1][0]
      px.push(`${s.sx(last).toFixed(1)},${s.zeroY.toFixed(1)}`)
      px.push(`${s.sx(first).toFixed(1)},${s.zeroY.toFixed(1)}`)
      return { sign: r.sign, polygon: px.join(' ') }
    })
}

function signOf(pts: [number, number][]): 'pos' | 'neg' {
  let maxAbs = 0
  let sign: 'pos' | 'neg' = 'pos'
  for (const [, y] of pts) {
    if (Math.abs(y) > maxAbs) {
      maxAbs = Math.abs(y)
      sign = y >= 0 ? 'pos' : 'neg'
    }
  }
  return sign
}
