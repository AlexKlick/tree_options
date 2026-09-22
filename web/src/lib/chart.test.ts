import { describe, expect, it } from 'vitest'
import { tipTransform } from './chart'

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
