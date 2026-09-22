import { describe, expect, it } from 'vitest'
import { clamp, interpAt, nearestIndex } from './interp'

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
