import { describe, expect, it } from 'vitest'
import { ageSeconds, ago, compactCount, etTime, usd, usdSigned } from './format'

// M8 flash review: "account 16608s ago" next to "marks 487m ago" - five
// ad-hoc age formatters, two of them seconds-only. One formatter now.
describe('ago', () => {
  it('picks the unit a person reads at a glance', () => {
    expect(ago(0)).toBe('0s')
    expect(ago(42)).toBe('42s')
    expect(ago(89)).toBe('89s')
    expect(ago(90)).toBe('2m')
    expect(ago(487 * 60)).toBe('8h 7m')
    expect(ago(16608)).toBe('4h 37m')
    expect(ago(3 * 3600)).toBe('3h')
    expect(ago(33243)).toBe('9h 14m')
    expect(ago(50 * 3600)).toBe('2d 2h')
    expect(ago(4 * 86400)).toBe('4d')
  })
})

describe('format', () => {
  it('mirrors the server _usd pins', () => {
    expect(usdSigned(17395)).toBe('+$17,395')
    expect(usdSigned(-105)).toBe('-$105')
    expect(usdSigned(0)).toBe('+$0')
    expect(usd(477)).toBe('$477')
  })

  it('formats ET regardless of the host timezone', () => {
    // 2026-09-22T16:09:35Z is 12:09:35 in New York (EDT, UTC-4).
    expect(etTime('2026-09-22T16:09:35Z')).toBe('12:09')
  })

  it('computes ages and tolerates null', () => {
    const now = Date.parse('2026-09-22T16:00:00Z')
    expect(ageSeconds('2026-09-22T15:59:30Z', now)).toBe(30)
    expect(ageSeconds(null, now)).toBeNull()
    expect(ageSeconds('garbage', now)).toBeNull()
  })

  it('compacts volume-scale counts (1.2M / 940K / 1,234)', () => {
    expect(compactCount(1_234_567)).toBe('1.2M')
    expect(compactCount(2_000_000)).toBe('2M')
    expect(compactCount(12_500_000)).toBe('12.5M')
    expect(compactCount(940_000)).toBe('940K')
    expect(compactCount(15_000)).toBe('15K')
    expect(compactCount(1_234)).toBe('1,234')
    expect(compactCount(9_999)).toBe('9,999')
    expect(compactCount(0)).toBe('0')
  })
})
