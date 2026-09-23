import type {
  DiscoveryResponse,
  GatewayStatus,
  PlanDetailResponse,
  PlansResponse,
  ScanRequestResponse,
  ScenarioDoc,
  MarketResponse,
  StatsResponse,
  SymbolDetail,
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

export const getStats = (): Promise<StatsResponse> => fetchJson('api/stats')

export const getGateway = (): Promise<GatewayStatus> => fetchJson('api/gateway')

export const requestScan = (): Promise<ScanRequestResponse> =>
  fetchJson('api/discovery/scan', { method: 'POST' })

export const getMarket = (): Promise<MarketResponse> => fetchJson('api/market')

export const getSymbol = (sym: string): Promise<SymbolDetail> =>
  fetchJson(`api/market/${encodeURIComponent(sym)}`)

export const requestMarketRefresh = (
  symbols?: string[],
): Promise<{ accepted: boolean; request_id: string }> =>
  fetchJson('api/market/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(symbols ? { symbols } : {}),
  })

export const watchOp = (
  op: 'add' | 'remove',
  symbol: string,
): Promise<{ accepted: boolean; request_id: string }> =>
  fetchJson('api/market/watch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ op, symbol }),
  })

/** Operator decision on an LLM proposal (idempotent runner-side). */
export const decideProposal = (
  op: 'approve' | 'dismiss',
  proposalId: string,
): Promise<{ accepted: boolean; request_id: string }> =>
  fetchJson('api/market/watch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ op, proposal_id: proposalId }),
  })

export const requestProposals = (): Promise<{ accepted: boolean; request_id: string }> =>
  fetchJson('api/market/propose', { method: 'POST' })

// Valuation scenario (M4): only the structure KEY crosses the wire; the
// runner resolves prices from its own artifacts.
export const requestScenario = (
  key: string,
): Promise<{ accepted: boolean; request_id: string; key: string }> =>
  fetchJson('api/discovery/backtest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key }),
  })

export const getScenario = (key: string): Promise<ScenarioDoc> =>
  fetchJson(`api/discovery/backtest?key=${encodeURIComponent(key)}`)
