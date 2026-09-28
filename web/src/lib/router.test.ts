import { describe, expect, it } from 'vitest'
import { parseHash, serialize } from './router'

describe('router with discover route', () => {
  it('parses the discover hash', () => {
    expect(parseHash('#/discover')).toEqual({ view: 'discover' })
  })

  it('serializes the discover route', () => {
    expect(serialize({ view: 'discover' })).toBe('#/discover')
  })

  it('keeps the spectator floor on a document-relative hash route', () => {
    expect(parseHash('#/trade-floor')).toEqual({ view: 'trade-floor' })
    expect(serialize({ view: 'trade-floor' })).toBe('#/trade-floor')
  })

  it('plan and list routes still parse', () => {
    expect(parseHash('#/plan/x')).toEqual({ view: 'plan', id: 'x' })
    expect(parseHash('')).toEqual({ view: 'list' })
    expect(parseHash('#/discoveryism')).toEqual({ view: 'list' }) // exact match only
  })
})
