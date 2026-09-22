import type {
  DiscoveryResponse,
  PlanDetailResponse,
  PlansResponse,
  ScanRequestResponse,
} from './types'

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { Accept: 'application/json', ...init?.headers },
  })
  const contentType = response.headers.get('content-type') ?? ''
  if (!response.ok || !contentType.includes('application/json')) {
    // The portal's anonymous 403 is an HTML sign-in page and a network
    // drop is a network drop — both mean "no data". Never parse or
    // render non-JSON bodies.
    throw new Error(`fetch ${url} failed: ${response.status}`)
  }
  return (await response.json()) as T
}

// Relative-only paths: they resolve under / (loopback) and /trex/
// (portal-stripped) alike. Never a leading slash.
export const getPlans = (): Promise<PlansResponse> => fetchJson('api/plans')

export const getPlan = (id: string): Promise<PlanDetailResponse> =>
  fetchJson(`api/plans/${encodeURIComponent(id)}`)

export const getDiscovery = (): Promise<DiscoveryResponse> => fetchJson('api/discovery')

export const requestScan = (): Promise<ScanRequestResponse> =>
  fetchJson('api/discovery/scan', { method: 'POST' })
