import { describe, expect, it } from 'vitest'
import { chartGeometry, endLabel, labelWidth, tipTransform } from './chart'

// M8 flash review: "$1,000,270" drew as "$1,000,27" and "$1,000,282" lost
// its "$" (fixed 64-unit gutters), and 12-unit text scaled to ~6px on a
// 336px phone chart (fixed 640-unit viewBox).
describe('chartGeometry', () => {
  it('draws at the measured pixel width so 12px text stays 12px', () => {
    expect(chartGeometry(336, ['$1,000,282']).vw).toBe(336)
    expect(chartGeometry(2255, ['$1,000,282']).vw).toBe(2255)
  })

  it('sizes the left gutter to the widest y label', () => {
    const g = chartGeometry(336, ['$1,000,282', '$0'])
    expect(g.padL).toBeGreaterThanOrEqual(labelWidth('$1,000,282') + 8)
  })

  it('keeps a readable height without growing to the width', () => {
    expect(chartGeometry(336, ['$1']).vh).toBeGreaterThanOrEqual(200)
    expect(chartGeometry(2255, ['$1']).vh).toBeLessThanOrEqual(320)
  })
})

describe('endLabel', () => {
  it('sits right of the dot when it fits', () => {
    expect(endLabel(200, '$5', 640)).toEqual({ x: 208, anchor: 'start' })
  })

  it('flips inside the chart when it would run off the right edge', () => {
    const at = endLabel(620, '$1,000,270', 640)
    expect(at.anchor).toBe('end')
    expect(at.x).toBeLessThanOrEqual(612)
  })
})

describe('tipTransform', () => {
  it('places the tooltip to the right of the crosshair early in the chart', () => {
    expect(tipTransform(100, 900)).toBe('translateX(12px)')
  })

  it('flips to the left once past the flip threshold', () => {
    expect(tipTransform(700, 900)).toBe('translateX(calc(-100% - 12px))')
  })

  it('uses the exact crosshair position against the chart width', () => {
    // 660/1000 == 0.66 exactly -> not past the threshold
    expect(tipTransform(660, 1000)).toBe('translateX(12px)')
    expect(tipTransform(660.01, 1000)).toBe('translateX(calc(-100% - 12px))')
  })
})
