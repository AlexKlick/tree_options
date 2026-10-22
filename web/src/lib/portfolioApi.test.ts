import { afterEach, describe, expect, it, vi } from 'vitest'
import { API_PATHS, type Holding } from './contracts'
import { MISMATCH_LABELS, centsText, getPortfolio, holdingSplit, microsText, symbolFor } from './portfolioApi'
import { samplePortfolio } from './portfolioSample'

const wave2 = import.meta.glob('../../../tests/contracts/fixtures/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>
const holding = structuredClone(wave2['../../../tests/contracts/fixtures/holding.json']) as Holding

afterEach(() => vi.unstubAllGlobals())

describe('getPortfolio', () => {
  it('reads trex-portfolio/1 relative to the mount', async () => {
    const urls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      urls.push(url)
      return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })
    }))
    await getPortfolio()
    expect(urls).toEqual([API_PATHS.portfolio.slice(1)])
  })
})

describe('integer money', () => {
  it('formats cents exactly, sign first', () => {
    expect(centsText(0)).toBe('$0.00')
    expect(centsText(5)).toBe('$0.05')
    expect(centsText(2500000)).toBe('$25,000.00')
    expect(centsText(-134301)).toBe('-$1,343.01')
  })

  it('rounds micros to the cent half away from zero', () => {
    expect(microsText(1_234_565_000)).toBe('$1,234.57')
    expect(microsText(1_234_564_999)).toBe('$1,234.56')
    expect(microsText(-5_000)).toBe('-$0.01')
    expect(microsText(-4_999)).toBe('$0.00')
    expect(microsText(0)).toBe('$0.00')
  })

  it('refuses to print a non-integer amount as money', () => {
    expect(centsText(1.5)).toBe('not a whole number of cents')
    expect(microsText(Number.NaN)).toBe('not a whole number of micros')
  })
})

describe('holdings', () => {
  it('splits a holding into TREX-attributed and outside quantities', () => {
    const split = holdingSplit({
      ...holding,
      quantity: 5,
      lots: [
        { attribution: { intent_id: 'eq-1', strategy_version: 's1', program_id: 'p1', sleeve_ref: 'unassigned' }, quantity: 3 },
        { attribution: { intent_id: null, strategy_version: null, program_id: null, sleeve_ref: 'unassigned' }, quantity: 2 },
      ],
    })
    expect(split).toEqual({ trex: 3, outside: 2 })
    expect(holdingSplit(holding).trex + holdingSplit(holding).outside).toBe(holding.quantity)
  })

  it('names a mismatched contract from holdings or outside positions, else null', () => {
    const view = samplePortfolio()
    expect(symbolFor(view, view.holdings.rows![0].con_id)).toBe(view.holdings.rows![0].symbol)
    expect(symbolFor(view, view.outside_positions.rows![0].con_id)).toBe(view.outside_positions.rows![0].symbol)
    expect(symbolFor(view, 999_999_999)).toBeNull()
  })

  it('describes every mismatch kind', () => {
    expect(Object.keys(MISMATCH_LABELS).sort()).toEqual(['missing', 'quantity_mismatch', 'unexplained'])
  })
})
