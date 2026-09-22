// Hash routing: the app must work at any mount (loopback / and the
// portal's prefix-stripped /trex/) without knowing its base path.

export type Route = { view: 'list' } | { view: 'plan'; id: string } | { view: 'discover' }

export function parseHash(hash: string): Route {
  if (hash === '#/discover') return { view: 'discover' }
  const m = /^#\/plan\/([^/]+)$/.exec(hash)
  return m ? { view: 'plan', id: decodeURIComponent(m[1]) } : { view: 'list' }
}

export function serialize(route: Route): string {
  if (route.view === 'discover') return '#/discover'
  return route.view === 'plan'
    ? `#/plan/${encodeURIComponent(route.id)}`
    : '#/'
}

export function navigate(route: Route): void {
  location.hash = serialize(route)
}
