import { describe, expect, it } from 'vitest'
import { payoffRegions, type PixelScale } from './regions'

// Fake linear scale: price maps to itself, pnl 5000..-1050 maps to
// 450..510.5, zero sits at 500 (avoids the -0 string artifact).
const s: PixelScale = {
  sx: (v) => v,
  sy: (v) => 500 - v / 100,
  zeroY: 500,
}

const stick: [number, number][] = [
  [100, 5000],
  [150, 5000],
  [170.21, 0],
  [250, -1050],
]

describe('payoffRegions', () => {
  it('splits at the breakeven into pos-then-neg polygons closed on the zero line', () => {
    const r = payoffRegions(stick, s)
    expect(r.map((x) => x.sign)).toEqual(['pos', 'neg'])
    // pos run rides the line, then closes back along zeroY to its start
    expect(r[0].polygon.startsWith('100.0,450.0')).toBe(true)
    expect(r[0].polygon.endsWith('100.0,500.0')).toBe(true)
    // neg run spans BE..250 with the max-loss corner included
    expect(r[1].polygon).toContain('250.0,510.5')
    expect(r[1].polygon).toContain('170.2,500.0')
  })

  it('drops degenerate series', () => {
    expect(payoffRegions([], s)).toEqual([])
    expect(payoffRegions([[1, 0]], s)).toEqual([])
    expect(
      payoffRegions(
        [
          [1, 0],
          [2, 0],
        ],
        s,
      ),
    ).toEqual([])
  })
})
