// Hash routing: the app must work at any mount (loopback / and the
// portal's prefix-stripped /trex/) without knowing its base path.

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

export function parseHash(hash: string): Route {
  if (hash === '#/discover') return { view: 'discover' }
  if (hash === '#/stats') return { view: 'stats' }
  if (hash === '#/market') return { view: 'market' }
  if (hash === '#/research') return { view: 'research' }
  if (hash === '#/action-model') return { view: 'action-model' }
  if (hash === '#/quant') return { view: 'quant' }
  if (hash === '#/workspace') return { view: 'workspace' }
  if (hash === '#/trade-floor') return { view: 'trade-floor' }
  const sym = /^#\/market\/([A-Z.]{1,6})$/.exec(hash)
  if (sym) return { view: 'symbol', id: sym[1] }
  const m = /^#\/plan\/([^/]+)$/.exec(hash)
  return m ? { view: 'plan', id: decodeURIComponent(m[1]) } : { view: 'list' }
}

export function serialize(route: Route): string {
  if (route.view === 'discover') return '#/discover'
  if (route.view === 'stats') return '#/stats'
  if (route.view === 'market') return '#/market'
  if (route.view === 'research') return '#/research'
  if (route.view === 'action-model') return '#/action-model'
  if (route.view === 'quant') return '#/quant'
  if (route.view === 'workspace') return '#/workspace'
  if (route.view === 'trade-floor') return '#/trade-floor'
  if (route.view === 'symbol') return `#/market/${route.id}`
  return route.view === 'plan'
    ? `#/plan/${encodeURIComponent(route.id)}`
    : '#/'
}

export function navigate(route: Route): void {
  location.hash = serialize(route)
}
