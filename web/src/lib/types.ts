// Mirrors the Python API contract (src/tree_options/trex_web/app.py).
// Plain numbers + ISO strings from the server; this app formats.

export type WindowState = 'before' | 'during' | 'after' | 'wrong_day'
export type StructureStatus =
  | 'planned'
  | 'enter_working'
  | 'open'
  | 'exit_working'
  | 'closed'

export interface PlanSummary {
  id: string
  account_mode: string
  entry_date: string
  entry_window_start: string
  entry_window_end: string
  structure_count: number
  total_debit_cap: number
  committed_at_caps: number
  state_present: boolean
  armed: boolean
  heartbeat: string | null
  worst_state: StructureStatus | null
  window_state: WindowState
  open_qty: number
  days_to_expiry: Record<string, number>
  days_to_deadline: Record<string, number>
  unrealized_open: number | null
  unrealized_filled: number | null
  realized: number | null
}

export interface PortfolioModeBucket {
  open_qty: number
  committed_filled: number
  unrealized_open: number
  unrealized_filled: number
  realized: number
}

export interface PortfolioBlock {
  plans_count: number
  plans_with_state: number
  open_qty: number
  committed_at_caps: number
  committed_filled: number
  unrealized_open: number | null
  unrealized_filled: number | null
  realized: number | null
  realized_partial_count: number
  marks_age_seconds: number | null
  marks_stale: boolean
  worst_state: string | null
  by_account_mode: Record<string, PortfolioModeBucket>
}

export interface AccountBlock {
  account_id: string
  net_liquidation: number
  cash: number
  buying_power: number
  currency: string
  ts: string
  age_seconds: number | null
  source: string
}

export interface PlansResponse {
  now: string
  gateway_reachable: boolean
  plans: PlanSummary[]
  portfolio: PortfolioBlock
  /** Portfolio-level net exposure; null during the API restart window. */
  net_positions: NetPosition[] | null
  account: AccountBlock | null
  accounts_seen: string[]
}

export interface RuleResultRow {
  rule: string
  status: string
  detail: string
}

export interface CandidateRow {
  underlying: string
  expiry: string
  dte: number
  short_strike: number
  long_strike: number
  width: number
  debit_mid: number | null
  debit_bid: number | null
  debit_ask: number | null
  short_mid: number | null
  long_mid: number | null
  short_delta: number | null
  long_delta: number | null
  short_spread_frac: number | null
  long_spread_frac: number | null
  yield_ratio: number | null
  max_profit: number | null
  max_loss: number | null
  target_mode_used: string
  rank: number
  accepted: boolean
  rules: RuleResultRow[]
  reasons: string[]
}

export interface DiscoveryDataQuality {
  underlyings_requested: number
  underlyings_scanned: number
  chains_available: boolean
  greeks_available: boolean
  expiries_scanned: number
  rows_quoted: number
  rows_unquoted: number
  notes: string[]
}

export interface DiscoveryLatest {
  run_id: string
  generated_at: string | null
  age_seconds: number | null
  mode: string
  request_id: string | null
  git_sha: string
  config_hash: string
  effective_target_modes: string[]
  data_quality: DiscoveryDataQuality
  candidates: CandidateRow[]
  rejected: CandidateRow[]
}

export interface DiscoveryRun {
  run_id: string
  generated_at: string | null
  scanned: number
  accepted: number
  rejected: number
}

export interface DiscoverySpool {
  pending: boolean
  last_result: {
    request_id: string
    status: string
    run_id?: string
    started_at?: string
    finished_at?: string
    detail?: string
  } | null
}

export interface DiscoveryConfig {
  underlyings: string[]
  dte_min: number
  dte_max: number
  widths: number[]
  target_mode: string
  target_delta: number
  target_otm_frac: number
  delta_band: number[]
  min_debit: number
  max_leg_spread_frac: number
  max_candidates_per_underlying: number
  max_candidates_total: number
}

export interface ShadowPosition {
  episode_id: string
  key: string
  underlying: string
  expiry: string
  short_strike: number
  long_strike: number
  width: number
  qty: number
  debit_paid: number
  opened_at: string
  opened_run_id: string
  status: string
  last_mark: number | null
  last_mark_at: string | null
  mark_source: string
  best_pnl: number | null
  worst_pnl: number | null
  final_pnl: number | null
  pnl: number | null
}

export interface ShadowStats {
  open: number
  expired: number
  mean_pnl: number | null
  hit_rate: number | null
  not_executed: boolean
}

export interface ShadowBlock {
  version: number
  positions: ShadowPosition[]
  stats: ShadowStats
  age_seconds: number | null
}

export interface DiscoveryResponse {
  now: string
  state_dir: string
  config_present: boolean
  config: DiscoveryConfig | null
  latest: DiscoveryLatest | null
  shadow: ShadowBlock | null
  runs: DiscoveryRun[]
  spool: DiscoverySpool
}

export interface ScanRequestResponse {
  accepted: boolean
  request_id: string
  request_ts: string
  spool_pending: boolean
  note: string
}

export interface PlanStructureSpec {
  id: string
  underlying: string
  entry_date: string
  expiry: string
  exit_deadline: string
  long_strike: number
  short_strike: number
  width: number
  quantity: number
  limit_cap: number
  days_to_expiry: number
  days_to_deadline: number
}

export interface Runbook {
  now_et: string
  entry_date: string
  window_state: WindowState
  monitor_armed: boolean
  heartbeat: string | null
  deadline_breached: boolean
  expiry_within_a_week: boolean
  days_to_exit_deadline: Record<string, number>
  days_to_expiry: Record<string, number>
}

export interface StructureStateView {
  state: StructureStatus
  entry_fill: number | null
  filled_qty: number
  open_qty: number
  exit_fill: number | null
  exit_filled_qty: number
  entry_cycles: number
  exit_cycles: number
  exit_reason: string | null
  close_reason: string | null
  touch_ts: string | null
  updated_at: string | null
  realized_pnl: number | null
}

export interface MarkRow {
  qty: number | null
  entry: number | null
  bid: number | null
  ask: number | null
  mark: number | null
  unrealized: number | null
}

export interface Marks {
  ts: string | null
  age_seconds: number | null
  total_unrealized: number | null
  spots: Record<string, number>
  structures: Record<string, MarkRow>
}

export interface BookSummary {
  committed: number
  max_gain: number
  max_loss: number
  short_floor: number | null
}

export interface NetPositionLeg {
  structure_id: string
  expiry: string
  long_strike: number
  short_strike: number
  open_qty: number
  entry: number
}

export interface NetPosition {
  underlying: string
  structure_count: number
  open_qty: number
  avg_entry: number | null
  committed: number
  max_gain: number
  max_loss: number
  unrealized: number | null
  short_floor: number
  long_ceiling: number
  legs: NetPositionLeg[]
}

export interface Payoff {
  structure_id: string
  underlying: string
  view: { x_lo: number; x_hi: number }
  points: [number, number][]
  levels: {
    long_strike: number
    short_strike: number
    entry: number
    qty: number
    width: number
    breakeven: number
    max_gain: number
    max_loss: number
    spot: number | null
  }
  labels: { max_gain: string; max_loss: string }
}

export interface HistorySeries {
  points: [number, number][]
  y_lo: number
  y_hi: number
  last: { ts_ms: number; pnl?: number; value?: number; pos: boolean }
}

export interface StatsDay {
  date: string
  realized: number
  unrealized_eod: number | null
  total: number | null
}

export interface StatsTotals {
  realized: number
  unrealized_last: number | null
  wins: number
  losses: number
  win_rate: number | null
  best_day: number | null
  worst_day: number | null
  plans_tracked: number
  structures_closed: number
}

export interface StatsPlanRow {
  plan_id: string
  realized: number
  unrealized_last: number | null
  structures_closed: number
  first_ts: string | null
  last_ts: string | null
}

export interface StatsStructureRow {
  plan_id: string
  structure_id: string
  underlying: string
  status: string
  entry_fill: number | null
  filled_qty: number
  realized: number | null
}

export interface StatsResponse {
  now: string
  tracking_since: string | null
  equity_account: string | null
  equity: HistorySeries | null
  days: StatsDay[]
  totals: StatsTotals
  per_plan: StatsPlanRow[]
  per_structure: StatsStructureRow[]
}

export type EventRecord = Record<string, unknown> & { ts?: string; event?: string }

export interface PlanDetailResponse {
  now: string
  gateway_reachable: boolean
  plan: {
    id: string
    account_mode: string
    entry_window_start: string
    entry_window_end: string
    total_debit_cap: number
    committed_at_caps: number
    structures: PlanStructureSpec[]
  }
  runbook: Runbook
  state_present: boolean
  structures: Record<string, StructureStateView>
  marks: Marks | null
  book_summary: BookSummary | null
  net_positions: NetPosition[]
  payoffs: Payoff[]
  history: HistorySeries | null
  events: EventRecord[]
}
