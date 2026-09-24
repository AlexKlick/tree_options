import { describe, expect, it } from 'vitest'
import { clamp, gapBreakMs, interpAt, nearestIndex, splitAtGaps } from './interp'

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
