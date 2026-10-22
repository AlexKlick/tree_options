import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { legacyRoute, navigate, parseHash, serialize, useRoute, type Route } from './router'
import { resolveHash } from './routes'

// The alias table, written out by hand (an oracle independent of routes.ts):
// every hash the pre-Wave-3 app answered, the view it rendered then, and the
// destination it sits under now (docs/product/WAVE3-CONTRACTS.md C3.3 and
// default 16). '#/' and '' are the only hashes whose target changed: they
// open Today, and the Plans list moved to '#/plans'.
const ALIASES: ReadonlyArray<[hash: string, route: Route, destination: string]> = [
  ['#/plans', { view: 'list' }, 'portfolio'],
  ['#/plan/spy-put-spread', { view: 'plan', id: 'spy-put-spread' }, 'portfolio'],
  ['#/plan/spy%20put%2Fspread', { view: 'plan', id: 'spy put/spread' }, 'portfolio'],
  ['#/stats', { view: 'stats' }, 'portfolio'],
  ['#/market', { view: 'market' }, 'trade-review'],
  ['#/market/SPY', { view: 'symbol', id: 'SPY' }, 'trade-review'],
  ['#/market/BRK.B', { view: 'symbol', id: 'BRK.B' }, 'trade-review'],
  ['#/discover', { view: 'discover' }, 'experiments'],
  ['#/research', { view: 'research' }, 'experiments'],
  ['#/action-model', { view: 'action-model' }, 'experiments'],
  ['#/quant', { view: 'quant' }, 'experiments'],
  ['#/workspace', { view: 'workspace' }, 'experiments'],
  ['#/trade-floor', { view: 'trade-floor' }, 'experiments'],
  ['#/experiments', { view: 'experiments' }, 'experiments'],
  ['#/experiments/qsl:observation', { view: 'experiments', id: 'qsl:observation' }, 'experiments'],
]

// Wave 3 pages render new components, never a pre-Wave-3 view.
const WAVE3_HASHES = [
  '',
  '#',
  '#/',
  '#/today',
  '#/trade-review',
  `#/trade-review/${'a'.repeat(64)}`,
  '#/portfolio',
  '#/activity',
  '#/activity/acceptance',
  '#/activity/settings',
  '#/pilot',
  '#/pilot/eq-baseline-spy',
  '#/onboarding',
]

afterEach(() => {
  history.replaceState(null, '', '#')
})

describe('alias table: every pre-Wave-3 hash still resolves', () => {
  it.each(ALIASES)('%s renders its old view under its destination', (hash, route, destination) => {
    const resolved = resolveHash(hash)
    expect(resolved.fallback).toBe(false)
    expect(resolved.destination).toBe(destination)
    expect(legacyRoute(resolved)).toEqual(route)
    expect(parseHash(hash)).toEqual(route)
  })

  it.each(ALIASES)('%s round-trips through serialize', (_hash, route) => {
    expect(parseHash(serialize(route))).toEqual(route)
  })

  it('the Plans list lives at #/plans now; the root opens Today', () => {
    expect(serialize({ view: 'list' })).toBe('#/plans')
    for (const hash of ['', '#', '#/']) {
      const resolved = resolveHash(hash)
      expect([resolved.entry.id, resolved.fallback, legacyRoute(resolved)]).toEqual(['today', false, null])
    }
  })

  it.each(WAVE3_HASHES)('%s is a Wave 3 page, not an old view', (hash) => {
    const resolved = resolveHash(hash)
    expect(resolved.fallback).toBe(false)
    expect(legacyRoute(resolved)).toBeNull()
  })

  it('unknown and malformed hashes fall back to Today without a legacy view', () => {
    for (const hash of ['#/discoveryism', '#/nope', '#/plan/', '#/market/spy', '#/trade-review/xyz', '#/plans/']) {
      const resolved = resolveHash(hash)
      expect([resolved.entry.id, resolved.fallback], hash).toEqual(['today', true])
      expect(legacyRoute(resolved), hash).toBeNull()
    }
    // a malformed escape still opens the plan (raw id), never throws
    expect(parseHash('#/plan/%E0%A4%A')).toEqual({ view: 'plan', id: '%E0%A4%A' })
    // the legacy parser keeps its old answer for a hash with no old view
    expect(parseHash('#/discoveryism')).toEqual({ view: 'list' })
  })

  it('serializes the discover and trade-floor routes as before', () => {
    expect(serialize({ view: 'discover' })).toBe('#/discover')
    expect(serialize({ view: 'trade-floor' })).toBe('#/trade-floor')
    expect(serialize({ view: 'plan', id: 'a b' })).toBe('#/plan/a%20b')
    expect(serialize({ view: 'symbol', id: 'SPY' })).toBe('#/market/SPY')
    expect(serialize({ view: 'experiments', id: 'study-1' })).toBe('#/experiments/study-1')
  })
})

describe('useRoute', () => {
  it('follows hash changes', async () => {
    history.replaceState(null, '', '#/market')
    const { result } = renderHook(() => useRoute())
    expect([result.current.entry.id, result.current.destination, result.current.hash]).toEqual([
      'market',
      'trade-review',
      '#/market',
    ])
    await act(async () => {
      navigate({ view: 'list' })
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect([result.current.entry.id, result.current.destination, result.current.hash]).toEqual([
      'plans',
      'portfolio',
      '#/plans',
    ])
    await act(async () => {
      history.replaceState(null, '', '#/no-such-page')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect([result.current.entry.id, result.current.fallback, result.current.hash]).toEqual([
      'today',
      true,
      '#/no-such-page',
    ])
  })
})
