import type { PlanDetailResponse, PlansResponse } from './types'

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: { Accept: 'application/json' } })
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
