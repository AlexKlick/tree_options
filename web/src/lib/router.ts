// Hash routing: the app must work at any mount (loopback / and the
// portal's prefix-stripped /trex/) without knowing its base path.
//
// The route table is data in lib/routes.ts (C3.3 of
// docs/product/WAVE3-CONTRACTS.md); this module is the React side of it
// (useRoute) plus the pre-Wave-3 `Route` vocabulary the old views and their
// links still speak. Every old hash resolves to its old view through the
// table's `legacyView` column; '#/' opens Today now and the Plans list lives
// at '#/plans'.

import { useMemo, useSyncExternalStore } from 'react'
import { hashFor, resolveHash, type ResolvedRoute } from './routes'

export type Route =
  | { view: 'list' }
  | { view: 'plan'; id: string }
  | { view: 'discover' }
  | { view: 'stats' }
  | { view: 'market' }
  | { view: 'symbol'; id: string }
  | { view: 'research' }
  | { view: 'action-model' }
  | { view: 'trade-floor' }
  | { view: 'quant' }
  | { view: 'workspace' }
  | { view: 'experiments'; id?: string }

/** The pre-Wave-3 view a resolved route renders; null for a Wave 3 page or a fallback. */
export function legacyRoute(resolved: ResolvedRoute): Route | null {
  if (resolved.fallback) return null
  const view = resolved.entry.legacyView
  const param = resolved.param
  switch (view) {
    case null:
      return null
    case 'plan':
    case 'symbol':
      return param === null ? null : { view, id: param }
    case 'experiments':
      return param === null ? { view } : { view, id: param }
    default:
      return { view }
  }
}

/** The old view a hash names; `{ view: 'list' }` when it names none (the
 * pre-Wave-3 answer). Pages are chosen with `useRoute`, not with this. */
export function parseHash(hash: string): Route {
  return legacyRoute(resolveHash(hash)) ?? { view: 'list' }
}

export function serialize(route: Route): string {
  switch (route.view) {
    case 'list':
      return hashFor('plans')
    case 'plan':
      return hashFor('plan', route.id)
    case 'symbol':
      return hashFor('symbol', route.id)
    case 'experiments':
      return route.id ? hashFor('experiment', route.id) : hashFor('experiments')
    default:
      // the remaining views share their route id
      return hashFor(route.view)
  }
}

export function navigate(route: Route): void {
  location.hash = serialize(route)
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

function readHash(): string {
  return location.hash
}

/** The current `location.hash`, re-rendering on every change. */
export function useHash(): string {
  return useSyncExternalStore(subscribe, readHash, readHash)
}

export type CurrentRoute = ResolvedRoute & { hash: string }

/** The resolved current route (never throws; unknown hashes open Today). */
export function useRoute(): CurrentRoute {
  const hash = useHash()
  return useMemo(() => ({ ...resolveHash(hash), hash }), [hash])
}
