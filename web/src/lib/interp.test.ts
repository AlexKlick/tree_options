import { describe, expect, it } from 'vitest'
import {
  clamp,
  gapBreakMs,
  gapScale,
  interpAt,
  nearestIndex,
  scalePos,
  scaleTime,
  splitAtGaps,
} from './interp'

// Long put spread: flat max-gain plateau, one kink at the breakeven,
// flat max-loss plateau. The server emits the kink exactly.
const stick: [number, number][] = [
  [100, 5000],
  [170.21, 0],
  [250, -1050],
]

describe('nearestIndex', () => {
  it('finds exact, nearest, and clamped indices', () => {
    expect(nearestIndex([1, 3, 5], 1)).toBe(0)
    expect(nearestIndex([1, 3, 5], 4)).toBe(1)
    expect(nearestIndex([1, 3, 5], 99)).toBe(2)
    expect(nearestIndex([1, 3, 5], -5)).toBe(0)
    expect(nearestIndex([], 7)).toBe(0)
  })
})

describe('interpAt', () => {
  it('is exact at the emitted kink and flat past both ends', () => {
    expect(interpAt(stick, 170.21)).toBe(0)
    expect(interpAt(stick, 100)).toBe(5000)
    expect(interpAt(stick, 50)).toBe(5000)
    expect(interpAt(stick, 300)).toBe(-1050)
  })

  it('interpolates linearly between kinks', () => {
    const mid = 170.21 + (250 - 170.21) / 2
    expect(interpAt(stick, mid)).toBeCloseTo(-525, 1)
  })

  it('degenerates safely', () => {
    expect(interpAt([], 5)).toBe(0)
    expect(interpAt([[3, 7]], 99)).toBe(7)
  })
})

describe('clamp', () => {
  it('clamps into range', () => {
    expect(clamp(5, 0, 3)).toBe(3)
    expect(clamp(-2, 0, 3)).toBe(0)
    expect(clamp(2, 0, 3)).toBe(2)
  })
})

describe('gapBreakMs', () => {
  const S = 1_000
  // monitor cadence: 20s ticks, then an overnight hold
  const ticks: [number, number][] = [
    ...Array.from({ length: 20 }, (_, i) => [i * 20 * S, i] as [number, number]),
    ...Array.from({ length: 20 }, (_, i) => [17 * 3600 * S + i * 20 * S, i] as [number, number]),
  ]
  // daily bars across a weekend (Fri, Sat, Sun, Mon)
  const D = 86_400 * S
  const daily: [number, number][] = [
    [0, 1],
    [D, 2],
    [2 * D, 3],
    [3 * D, 4],
  ]

  it('is 30x the median spacing, floored at 2 minutes', () => {
    expect(gapBreakMs(ticks)).toBe(30 * 20 * S) // 10 min
    expect(gapBreakMs(daily)).toBe(30 * D) // 30 days: weekends never break
  })

  it('degenerates safely', () => {
    expect(gapBreakMs([])).toBe(Number.POSITIVE_INFINITY)
    expect(gapBreakMs([[0, 1]])).toBe(Number.POSITIVE_INFINITY)
    expect(gapBreakMs([[0, 1], [0, 2]])).toBe(Number.POSITIVE_INFINITY) // no positive deltas
  })
})

describe('splitAtGaps', () => {
  const S = 1_000
  const pts: [number, number][] = [
    [0, 10],
    [20 * S, 11],
    [40 * S, 12],
    [17 * 3600 * S, 13], // overnight hold
    [17 * 3600 * S + 20 * S, 14],
  ]

  it('splits only across the gap, keeping single-point runs', () => {
    const runs = splitAtGaps(pts, 30 * 20 * S)
    expect(runs.map((r) => r.length)).toEqual([3, 2])
    expect(runs[1][0][1]).toBe(13)
  })

  it('returns one run when there is no gap', () => {
    expect(splitAtGaps(pts, Number.POSITIVE_INFINITY).map((r) => r.length)).toEqual([5])
  })

  it('degenerates safely', () => {
    expect(splitAtGaps([], 1)).toEqual([])
    expect(splitAtGaps([[5, 1]], 1).map((r) => r.length)).toEqual([1])
  })
})

describe('gapScale', () => {
  const S = 1_000
  // two observed runs of equal 60s span, a 17h gap between them
  const two: [number, number][] = [
    ...Array.from({ length: 4 }, (_, i) => [i * 20 * S, i] as [number, number]),
    ...Array.from({ length: 4 }, (_, i) => [17 * 3600 * S + i * 20 * S, i] as [number, number]),
  ]

  it('is null for a single run (plain time-proportional axis)', () => {
    expect(gapScale(two, Number.POSITIVE_INFINITY)).toBeNull()
    expect(gapScale([], 1)).toBeNull()
  })

  it('allocates observed time proportionally with a small gap allowance', () => {
    const sc = gapScale(two, 30 * 20 * S)!
    expect(sc.runs.length).toBe(2)
    const [r1, r2] = sc.runs
    // equal spans -> equal width; 3% allowance between, none after the last
    expect(r1.a).toBe(0)
    expect(r2.b).toBeCloseTo(1, 9)
    expect(r2.a - r1.b).toBeCloseTo(0.03, 9)
    expect(r1.b - r1.a).toBeCloseTo(r2.b - r2.a, 9)
  })

  it('caps total gap allowance at 30% of the axis', () => {
    // 20 runs of equal span -> 19 gaps: allowance shrinks below the default
    const many: [number, number][] = []
    for (let r = 0; r < 20; r++) {
      for (let i = 0; i < 3; i++) many.push([(r * 3600 + i * 20) * S, i])
    }
    const sc = gapScale(many, 30 * 20 * S)!
    const voidTotal = sc.runs.slice(1).reduce((s, r, i) => s + (r.a - sc.runs[i].b), 0)
    expect(sc.runs[19].b).toBeCloseTo(1, 9)
    expect(voidTotal).toBeLessThanOrEqual(0.3 + 1e-9)
    // and every run still gets positive width
    for (const r of sc.runs) expect(r.b).toBeGreaterThan(r.a)
  })
})

describe('scalePos / scaleTime', () => {
  const S = 1_000
  const pts: [number, number][] = [
    [0, 10],
    [60 * S, 11],
    [20 * 3600 * S, 12],
    [20 * 3600 * S + 60 * S, 13],
  ]
  const sc = gapScale(pts, 30 * 60 * S)!

  it('maps run endpoints to their normalized slots and clamps outside', () => {
    expect(scalePos(sc, -5)).toBe(0)
    expect(scalePos(sc, pts[3][0])).toBeCloseTo(1, 9)
    const mid1 = scalePos(sc, 30 * S) // halfway through run 1
    expect(mid1).toBeGreaterThan(0)
    expect(mid1).toBeLessThan(sc.runs[0].b)
  })

  it('inverts its own forward map at every sample', () => {
    for (const [t] of pts) {
      expect(scaleTime(sc, scalePos(sc, t))).toBe(t)
    }
  })

  it('snaps a hover inside the gap allowance to the nearer run edge', () => {
    const inGap = (sc.runs[0].b + sc.runs[1].a) / 2
    expect(scaleTime(sc, inGap)).toBeGreaterThan(pts[1][0])
    expect(scaleTime(sc, sc.runs[0].b + 1e-6)).toBe(pts[1][0])
    expect(scaleTime(sc, sc.runs[1].a - 1e-6)).toBe(pts[2][0])
    expect(scaleTime(sc, -1)).toBe(pts[0][0])
    expect(scaleTime(sc, 2)).toBe(pts[3][0])
  })
})
