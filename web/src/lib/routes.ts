// TREX Wave 3 route table, as data (docs/product/WAVE3-CONTRACTS.md C3.3).
//
// Five destinations (Today, Experiments, Trade review, Portfolio, Activity &
// settings) on desktop and phone. Every hash route the app had before Wave 3
// (lib/router.ts, App.tsx) stays reachable: the old views are listed under the
// `advanced` group with the `legacyView` router.ts renders for them. `#/` and
// the empty hash now open Today; the old Plans list moved to `#/plans`.
// Unknown hashes fall back to Today (flagged `fallback`), as they used to fall
// back to Plans. Hash routing keeps the app mount-agnostic (loopback `/` and
// the portal's prefix-stripped `/trex/`).

export type DestinationId = 'today' | 'experiments' | 'trade-review' | 'portfolio' | 'activity'

export interface Destination {
  id: DestinationId
  /** Desktop navigation label. */
  label: string
  /** Phone tab bar label (short). */
  shortLabel: string
  /** The canonical hash. */
  hash: string
}

/** In navigation order (desktop sidebar/top bar and phone tab bar alike). */
export const DESTINATIONS: readonly Destination[] = [
  { id: 'today', label: 'Today', shortLabel: 'Today', hash: '#/today' },
  { id: 'experiments', label: 'Experiments', shortLabel: 'Experiments', hash: '#/experiments' },
  { id: 'trade-review', label: 'Trade review', shortLabel: 'Review', hash: '#/trade-review' },
  { id: 'portfolio', label: 'Portfolio', shortLabel: 'Portfolio', hash: '#/portfolio' },
  { id: 'activity', label: 'Activity & settings', shortLabel: 'Activity', hash: '#/activity' },
]

export const DEFAULT_DESTINATION: DestinationId = 'today'

/**
 * - `destination`: one of the five pages;
 * - `detail`: a page under a destination (highlights that destination);
 * - `flow`: a guided flow (onboarding), outside the tab bar;
 * - `advanced`: a pre-Wave-3 view, kept as an alias and listed in the
 *   advanced menu of its destination.
 */
export type RouteGroup = 'destination' | 'detail' | 'flow' | 'advanced'

/** The `Route['view']` values of lib/router.ts (pre-Wave-3 views). */
export type LegacyView =
  | 'list'
  | 'plan'
  | 'discover'
  | 'stats'
  | 'market'
  | 'symbol'
  | 'research'
  | 'action-model'
  | 'trade-floor'
  | 'quant'
  | 'workspace'
  | 'experiments'

export interface RouteEntry {
  /** Stable id; page components key on it. */
  id: string
  /** Documentation form, `:name` marks a parameter. */
  pattern: string
  /** Anchored; group 1 is the parameter when `param` is set. */
  regex: RegExp
  param: string | null
  group: RouteGroup
  /** The destination highlighted while this route is open. */
  destination: DestinationId
  label: string
  /** The pre-Wave-3 view this route renders (aliases), or null for new pages. */
  legacyView: LegacyView | null
  /** A hash that resolves to this route (tests and docs use it). */
  example: string
}

// Order matters: the first matching entry wins, so specific patterns precede
// the parameterised ones they could be mistaken for.
export const ROUTES: readonly RouteEntry[] = [
  // -- the five destinations ------------------------------------------------
  { id: 'today', pattern: '#/today', regex: /^#\/today$/, param: null, group: 'destination', destination: 'today', label: 'Today', legacyView: null, example: '#/today' },
  { id: 'experiments', pattern: '#/experiments', regex: /^#\/experiments$/, param: null, group: 'destination', destination: 'experiments', label: 'Experiments', legacyView: 'experiments', example: '#/experiments' },
  { id: 'trade-review', pattern: '#/trade-review', regex: /^#\/trade-review$/, param: null, group: 'destination', destination: 'trade-review', label: 'Trade review', legacyView: null, example: '#/trade-review' },
  { id: 'portfolio', pattern: '#/portfolio', regex: /^#\/portfolio$/, param: null, group: 'destination', destination: 'portfolio', label: 'Portfolio', legacyView: null, example: '#/portfolio' },
  { id: 'activity', pattern: '#/activity', regex: /^#\/activity$/, param: null, group: 'destination', destination: 'activity', label: 'Activity & settings', legacyView: null, example: '#/activity' },
  // -- Wave 3 detail pages and flows ----------------------------------------
  { id: 'trade-review-proposal', pattern: '#/trade-review/:proposalId', regex: /^#\/trade-review\/([0-9a-f]{64})$/, param: 'proposalId', group: 'detail', destination: 'trade-review', label: 'Proposal', legacyView: null, example: `#/trade-review/${'0'.repeat(64)}` },
  { id: 'activity-acceptance', pattern: '#/activity/acceptance', regex: /^#\/activity\/acceptance$/, param: null, group: 'detail', destination: 'activity', label: 'Paper acceptance', legacyView: null, example: '#/activity/acceptance' },
  { id: 'activity-settings', pattern: '#/activity/settings', regex: /^#\/activity\/settings$/, param: null, group: 'detail', destination: 'activity', label: 'Settings', legacyView: null, example: '#/activity/settings' },
  { id: 'pilot', pattern: '#/pilot', regex: /^#\/pilot$/, param: null, group: 'detail', destination: 'experiments', label: 'Research pilot', legacyView: null, example: '#/pilot' },
  { id: 'pilot-program', pattern: '#/pilot/:programId', regex: /^#\/pilot\/([A-Za-z0-9][A-Za-z0-9._-]{0,95})$/, param: 'programId', group: 'detail', destination: 'experiments', label: 'Pilot program', legacyView: null, example: '#/pilot/eq-baseline-spy' },
  { id: 'onboarding', pattern: '#/onboarding', regex: /^#\/onboarding$/, param: null, group: 'flow', destination: 'today', label: 'Getting started', legacyView: null, example: '#/onboarding' },
  // -- every pre-Wave-3 hash route, kept ------------------------------------
  { id: 'experiment', pattern: '#/experiments/:id', regex: /^#\/experiments\/([a-zA-Z0-9_.:-]{1,200})$/, param: 'id', group: 'detail', destination: 'experiments', label: 'Experiment', legacyView: 'experiments', example: '#/experiments/qsl:observation' },
  { id: 'plans', pattern: '#/plans', regex: /^#\/plans$/, param: null, group: 'advanced', destination: 'portfolio', label: 'Plans', legacyView: 'list', example: '#/plans' },
  { id: 'plan', pattern: '#/plan/:id', regex: /^#\/plan\/([^/]+)$/, param: 'id', group: 'advanced', destination: 'portfolio', label: 'Plan', legacyView: 'plan', example: '#/plan/spy-put-spread' },
  { id: 'stats', pattern: '#/stats', regex: /^#\/stats$/, param: null, group: 'advanced', destination: 'portfolio', label: 'Performance', legacyView: 'stats', example: '#/stats' },
  { id: 'market', pattern: '#/market', regex: /^#\/market$/, param: null, group: 'advanced', destination: 'trade-review', label: 'Market', legacyView: 'market', example: '#/market' },
  { id: 'symbol', pattern: '#/market/:sym', regex: /^#\/market\/([A-Z.]{1,6})$/, param: 'sym', group: 'advanced', destination: 'trade-review', label: 'Symbol', legacyView: 'symbol', example: '#/market/SPY' },
  { id: 'discover', pattern: '#/discover', regex: /^#\/discover$/, param: null, group: 'advanced', destination: 'experiments', label: 'Discover', legacyView: 'discover', example: '#/discover' },
  { id: 'research', pattern: '#/research', regex: /^#\/research$/, param: null, group: 'advanced', destination: 'experiments', label: 'Research Lab', legacyView: 'research', example: '#/research' },
  { id: 'action-model', pattern: '#/action-model', regex: /^#\/action-model$/, param: null, group: 'advanced', destination: 'experiments', label: 'Action model', legacyView: 'action-model', example: '#/action-model' },
  { id: 'quant', pattern: '#/quant', regex: /^#\/quant$/, param: null, group: 'advanced', destination: 'experiments', label: 'Quant lab', legacyView: 'quant', example: '#/quant' },
  { id: 'workspace', pattern: '#/workspace', regex: /^#\/workspace$/, param: null, group: 'advanced', destination: 'experiments', label: 'Research and paper', legacyView: 'workspace', example: '#/workspace' },
  { id: 'trade-floor', pattern: '#/trade-floor', regex: /^#\/trade-floor$/, param: null, group: 'advanced', destination: 'experiments', label: 'Trade floor', legacyView: 'trade-floor', example: '#/trade-floor' },
]

/** The hashes that open Today (the old `#/` Plans default is `#/plans` now). */
export const ROOT_HASHES: readonly string[] = ['', '#', '#/']

export interface ResolvedRoute {
  entry: RouteEntry
  /** The decoded parameter, or null. */
  param: string | null
  destination: DestinationId
  /** True when the hash matched nothing and Today was chosen. */
  fallback: boolean
}

function entryById(id: string): RouteEntry {
  const entry = ROUTES.find((r) => r.id === id)
  if (!entry) throw new Error(`route ${id} missing`)
  return entry
}

/** Resolve a `location.hash`; never throws for user input. */
export function resolveHash(hash: string): ResolvedRoute {
  if (ROOT_HASHES.includes(hash)) {
    return { entry: entryById('today'), param: null, destination: 'today', fallback: false }
  }
  for (const entry of ROUTES) {
    const match = entry.regex.exec(hash)
    if (!match) continue
    let param: string | null = null
    if (entry.param !== null) {
      try {
        param = decodeURIComponent(match[1])
      } catch {
        param = match[1]
      }
    }
    return { entry, param, destination: entry.destination, fallback: false }
  }
  return { entry: entryById('today'), param: null, destination: 'today', fallback: true }
}

/** The hash for a route id (with its parameter, URI-encoded for `plan`). */
export function hashFor(id: string, param?: string): string {
  const entry = entryById(id)
  if (entry.param === null) return entry.pattern
  if (param === undefined) throw new Error(`route ${id} needs :${entry.param}`)
  return entry.pattern.replace(`:${entry.param}`, id === 'plan' ? encodeURIComponent(param) : param)
}

/** The advanced (pre-Wave-3) views listed under a destination's menu. */
export function advancedFor(destination: DestinationId): RouteEntry[] {
  return ROUTES.filter((r) => r.group === 'advanced' && r.destination === destination && r.param === null)
}
