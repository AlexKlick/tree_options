import { describe, expect, it } from 'vitest'
import { ageSeconds, etTime, usd, usdSigned } from './format'

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
})
