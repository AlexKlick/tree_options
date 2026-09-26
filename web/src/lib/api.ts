import type {
  DiscoveryResponse,
  ExitMachineStatus,
  GatewayStatus,
  PlanDetailResponse,
  PlansResponse,
  ScanRequestResponse,
  ScenarioDoc,
  MarketResponse,
  StatsResponse,
  SymbolDetail,
  SymbolHistory,
  SymbolIdeas,
  SymbolOptions,
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

export const getExitMachine = (): Promise<ExitMachineStatus> => fetchJson('api/exit-machine')

export const requestScan = (): Promise<ScanRequestResponse> =>
  fetchJson('api/discovery/scan', { method: 'POST' })

export const getMarket = (): Promise<MarketResponse> => fetchJson('api/market')

export const getSymbol = (sym: string): Promise<SymbolDetail> =>
  fetchJson(`api/market/${encodeURIComponent(sym)}`)

/** Long-term panel history (ETag/304 keeps the 60 s re-poll free). Candles
 * read a reduced point count, so maxPoints rides the query string. */
export const getSymbolHistory = (
  sym: string,
  range: string,
  maxPoints = 600,
): Promise<SymbolHistory> =>
  fetchJson(
    `api/market/${encodeURIComponent(sym)}/history` +
      `?range=${encodeURIComponent(range)}&max_points=${maxPoints}`,
  )

/** Recorded options surface (cards + ATM term + slice) with a live slot
 * the discovery lane fills when its delayed-CBOE envelope warms. Rungs =
 * strike ladder width around ATM; expiries = nearest N expiries kept. */
export const getSymbolOptions = (
  sym: string,
  rungs = 5,
  expiries = 6,
): Promise<SymbolOptions> =>
  fetchJson(
    `api/market/${encodeURIComponent(sym)}/options` +
      `?window=${rungs}&max_expiries=${expiries}`,
  )

/** Advisory ideas surface: signals, the PROPOSED queue, paper positions,
 * the sealed scratch lane, and research context. */
export const getSymbolIdeas = (sym: string): Promise<SymbolIdeas> =>
  fetchJson(`api/market/${encodeURIComponent(sym)}/ideas`)

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

// --- RL-1 (research lab) — see TREX-Research-Lab-Design-and-Build-Handoff-29a9aa.md
// These are GET-only reads + POST-to-spool; the forecast/scenarios
// endpoints return 410 Gone (RL-3 / RL-2 out of scope).

import type {
  CandidateResultSummary,
  ComparisonRunResponse,
  ComparisonSpec,
  ResearchCandidatesResponse,
  ResearchEvidenceEnvelope,
  RunResultResponse,
} from './types'

export const listResearchCandidates = (filters?: {
  family?: string
  disposition?: string
  evidence_kind?: string
}): Promise<ResearchCandidatesResponse> => {
  const qs = new URLSearchParams()
  if (filters?.family) qs.set('family', filters.family)
  if (filters?.disposition) qs.set('disposition', filters.disposition)
  if (filters?.evidence_kind) qs.set('evidence_kind', filters.evidence_kind)
  const q = qs.toString()
  return fetchJson(`api/research/candidates${q ? `?${q}` : ''}`)
}

export const getResearchCandidate = (id: string): Promise<ResearchCandidatesResponse['candidates'][number]> =>
  fetchJson(`api/research/candidates/${encodeURIComponent(id)}`)

export const getResearchEvidence = (
  id: string,
  opts: { session?: string; as_of?: string } = {},
): Promise<ResearchEvidenceEnvelope> => {
  const qs = new URLSearchParams()
  if (opts.session) qs.set('session', opts.session)
  if (opts.as_of) qs.set('as_of', opts.as_of)
  const q = qs.toString()
  return fetchJson(`api/research/candidates/${encodeURIComponent(id)}/evidence${q ? `?${q}` : ''}`)
}

export const spoolComparison = (spec: ComparisonSpec): Promise<ComparisonRunResponse> =>
  fetchJson('api/research/compare', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(spec),
  })

/** Poll a run's lifecycle state (never triggers computation). */
export const getResearchRun = (run_id: string): Promise<ComparisonRunResponse> =>
  fetchJson(`api/research/runs/${encodeURIComponent(run_id)}`)

/** The recorded result — the lifecycle envelope; `result` is present
 * exactly when status === 'completed'. */
export const getComparisonResult = (run_id: string): Promise<RunResultResponse> =>
  fetchJson(`api/research/runs/${encodeURIComponent(run_id)}/result`)

// --- RL-2: reproducible scenario branching (handoff §10)
// Scenarios are FORKS of completed comparison runs. The body carries
// the diff over the parent's controls; the URL path carries the
// parent_run_id. The result record binds engine / input / calendar /
// scenario_diff shas for honest cache-key collision detection.

export const listScenarios = (
  parent_run_id?: string,
): Promise<{ scenarios: unknown[]; parent_run_id: string | null }> => {
  const qs = parent_run_id
    ? `?parent_run_id=${encodeURIComponent(parent_run_id)}`
    : ''
  return fetchJson(`api/research/scenarios${qs}`)
}

export const spoolScenario = (
  parent_run_id: string,
  body: { kind: string; access_mode?: string; diff?: Record<string, unknown> },
): Promise<{
  run_id: string
  status: string
  spec_hash: string
  parent_run_id: string
  workspace: string
}> =>
  fetchJson(`api/research/scenarios/${encodeURIComponent(parent_run_id)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

// Re-export for callers that already imported this from the old path.
export type { CandidateResultSummary as CandidateResult }
