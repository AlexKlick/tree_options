// Mirrors the Python API contract (src/tree_options/trex_web/app.py).
// Plain numbers + ISO strings from the server; this app formats.

// GET /api/gateway: the gateway watchdog's verdict (epoch seconds).
export type GatewayState =
  | 'ok'
  | 'starting'
  | 'checking'
  | 'needs_login'
  | 'needs_2fa'
  | 'api_down'
  | 'down'
  | 'unknown'
export interface GatewayStatus {
  status: GatewayState
  since: number | null
  detail: string | null
  checked_at: number | null
  login_url: string | null
  restarts_left: number | null
  next_restart_at: number | null
  ibc_phase: string | null
  api_ok: boolean | null
  vnc_running: boolean | null
  age_seconds: number | null
  watch_stale: boolean
  last_restart_at: number | null
}

// GET /api/exit-machine: the exit-machine (trex-monitor) watchdog's verdict.
export type ExitMachineState =
  | 'idle'
  | 'ok'
  | 'waiting_for_gateway'
  | 'touch_blind'
  | 'touch_suspended'
  | 'monitor_failing'
  | 'monitor_down'
  | 'unknown'
export interface ExitMachineBook {
  plan: string
  status: ExitMachineState
  detail: string
  heartbeat_age: number | null
}
export interface ExitMachineStatus {
  status: ExitMachineState
  since: number | null
  detail: string | null
  checked_at: number | null
  books: ExitMachineBook[]
  age_seconds: number | null
  watch_stale: boolean
}

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
  /** null while any filled structure's entry cost is unknown (R3-02). */
  committed_filled: number | null
  /** labelled priced-subtotal: never a whole-bucket claim */
  committed_known: number
  cost_unknown: boolean
  unpriced_qty: number
  unrealized_open: number
  unrealized_filled: number
  realized: number
}

export interface PortfolioBlock {
  plans_count: number
  plans_with_state: number
  open_qty: number
  committed_at_caps: number
  /** null while any filled structure's entry cost is unknown (R3-02). */
  committed_filled: number | null
  /** labelled priced-subtotal: never a whole-book claim */
  committed_known: number
  cost_unknown: boolean
  unpriced_qty: number
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
  /** price coverage (R3-02): nonzero = the side's average spans the priced
   * packages only; no whole-position cost/payoff may be derived */
  entry_unpriced_qty: number
  exit_unpriced_qty: number
}

export interface MarkRow {
  qty: number | null
  entry: number | null
  bid: number | null
  ask: number | null
  mark: number | null
  unrealized: number | null
  /** fills without a reported price: entry covers the priced subset only */
  unpriced: number | null
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
  entry: number | null
  entry_unpriced_qty: number
}

export interface NetPosition {
  underlying: string
  structure_count: number
  open_qty: number
  avg_entry: number | null
  /** null while any leg's entry cost is unknown (R3-02) */
  committed: number | null
  /** labelled priced-subtotal: never a whole-position claim */
  committed_known: number
  cost_unknown: boolean
  unpriced_qty: number
  max_gain: number | null
  max_loss: number | null
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

export interface MarketQuote {
  bid: number | null
  ask: number | null
  close: number | null
  iv30: number | null
  change_pct: number | null
  source_as_of: string | null
}

export interface MarketResponse {
  now: string
  last_refresh: string | null
  age_seconds: number | null
  watchlist: string[]
  symbols: Record<string, MarketQuote>
  errors: Record<string, string>
  watch_origins?: Record<string, string>
  proposals?: WatchProposal[]
  last_proposal_run?: ProposalRun | null
}

/** M6: an LLM watchlist suggestion awaiting the operator's decision. */
export interface WatchProposal {
  id: string
  symbol: string
  action: 'add' | 'remove'
  rationale: string
  confidence: number | null
  status: 'pending' | 'approved' | 'dismissed'
  created_at: string
  provenance?: { provider: string | null; model: string | null; trigger?: string }
}

export interface ProposalRun {
  at: string
  status: 'ok' | 'failed'
  provider: string | null
  model: string | null
  elapsed_s: number | null
  trigger: string
  notes: string[]
  added: number
}

export interface NewsItem {
  title: string
  link: string
  pub: string | null
  source: string
}

export interface SymbolDetail {
  now: string
  symbol: string
  quote: MarketQuote | null
  quote_age_seconds: number | null
  bars: HistorySeries | null
  bars_age_seconds?: number | null
  news: NewsItem[]
  news_age_seconds?: number | null
}

// GET /api/market/{sym}/history (symbol_history.py): long-term
// split-adjusted OHLCV from the desk's ohlc-panel.json (since 2021,
// extended nightly) — NOT the viewer's 365-day envelope. One row per
// session: [ET-midnight epoch ms, open, high, low, close, volume].
export type OhlcPoint = [number, number, number, number, number, number]

export interface SymbolHistory {
  now: string
  symbol: string
  source: string
  in_panel: boolean
  panel_last_session: string | null
  panel_sha256_12: string | null
  range: string
  /** Set once a range window was applied (in-panel responses only). */
  range_start?: string | null
  range_sessions?: number | null
  /** null + error = degrade (panel busy/unavailable); [] + in_panel=false
   * = symbol not in the panel yet (legacy envelope fallback). */
  points: OhlcPoint[] | null
  y_lo: number | null
  y_hi: number | null
  vol_max: number | null
  last: { date: string; ts_ms: number; close: number } | null
  /** Attached by the route on 200s only (age of the newest session). */
  history_age_seconds?: number | null
  note: string
  error: string | null
}

// GET /api/market/{sym}/options (options_view.py): the RECORDED desk
// surface (features cards + ATM term + chain slice around ATM), the long
// IV30 history, and — once the discovery lane's envelope warms — a live
// delayed-CBOE slice in the same row shape. Volatilities and moves are
// decimal fractions (0.25 = 25%); this app formats. Cards pass through
// the features JSON verbatim, so every card field is nullable and
// iv_rank degrades to its {status, reason} shape.
export interface OptionsSliceRow {
  exp: string
  dte: number
  right: string // "C" | "P"
  strike: number
  atm: boolean
  bid: number | null
  ask: number | null
  mid: number | null
  iv: number | null
  delta: number | null
  gamma: number | null
  theta: number | null
  vega: number | null
  oi: number | null
  volume: number | null
}

/** Live envelope slot: null until the discovery lane's viewchain warms. */
export interface OptionsLive {
  fetched_at: string
  age_seconds: number | null
  ttl_seconds: number | null
  spot: number | null
  slice: OptionsSliceRow[]
}

export interface IvRankCard {
  /** NOT_EVALUABLE degrade path (no IV30 / no history for the name). */
  status?: string
  reason?: string
  rank?: number | null
  percentile?: number | null
  n?: number
  low_n?: boolean
  outside_range?: string | null
  window?: number
  history_label?: string
}

export interface OptionsEarnings {
  next_report: string | null
  event_sessions?: string[]
  expiries?: string[]
  implied_move?: number | null
  implied_mean_abs_move?: number | null
  hist_mean_abs_move?: number | null
  hist_n?: number
  in_progress?: boolean
  reason?: string
  flag?: string
  schedule?: string
}

export interface OptionsCards {
  iv?: Record<string, number | null> // "30" | "60" | "90" | "180"
  iv_rank?: IvRankCard | null
  skew25?: Record<string, number | null> // "30" | "90"
  term_slope?: number | null // IV90/IV30 - 1
  yz22_ann?: number | null // Yang-Zhang 22d RV, annualized
  liquidity_score?: number | null
  earnings?: OptionsEarnings | null
}

export interface OptionsRecorded {
  session: string
  age_seconds: number | null
  spot: number | null
  cards: OptionsCards
  /** [expiry, dte, atm_iv, n_strikes, how] rows. */
  atm_term: [string, number, number, number, string][] | null
  slice: OptionsSliceRow[] | null
}

export interface Iv30History {
  /** [ET-midnight epoch ms, decimal iv30]. */
  points: [number, number][]
  y_lo: number
  y_hi: number
  n: number
  first: string
  last: string
  source: string
}

export interface SymbolOptions {
  now: string
  symbol: string
  available: boolean
  recorded: OptionsRecorded | null
  live: OptionsLive | null
  iv30_history: Iv30History | null
  warnings: string[]
}

// GET /api/market/{sym}/ideas: the advisory surface — signals (xsmom +
// PEAD), the miner's PROPOSED queue, paper positions, the sealed scratch
// lane, and research-ledger context. protocol is the boundary: only
// allowed_direction signals may point a trade; everything else is
// information. Every section is nullable and degrades honestly.
export interface XsmomCard {
  score: number | null  // null: name not in the sealed-36 ranking (e.g. PLTR/SPCX)
  in_top3: boolean
  top3: string[]
  is_rebalance_day: boolean
  n_ranked: number
  conventions_agree: boolean
}

export interface PeadBeat {
  report_date: string
  move: number
}

export interface PeadEvaluated {
  report_date: string
  prior_session: string | null
  move: number | null
  fires: boolean
  reason: string | null
}

export interface PeadCard {
  beats: PeadBeat[]
  evaluated: PeadEvaluated[]
}

export interface IdeasSignals {
  session: string
  age_seconds: number | null
  xsmom: XsmomCard
  pead: PeadCard
  next_report: string | null
}

export interface IdeasLeg {
  right: string
  action: string
  strike: number
  expiry: string
  bid: number | null
  ask: number | null
  oi: number | null
  iv: number | null
  delta: number | null
}

export interface IdeasSignalRef {
  name: string
  excess_20: number | null
}

export interface IdeasDeal {
  deal_id: string
  rank: number
  status: string
  row_title: string
  kind: string
  underlying: string
  quantity: number
  legs: IdeasLeg[]
  ref_mid: number | null
  fill: number | null
  limit: number | null
  max_loss: number | null
  signal: IdeasSignalRef | null
  reasons: string[]
  notes: string[]
}

export interface IdeasQueue {
  session: string
  entry_session: string
  valid_until: string
  miner_status: string // "PROPOSED" | ...
  deals: IdeasDeal[]
}

export interface IdeasPaperPosition {
  plan_id: string
  structure_id: string
  account_mode: string
  expiry: string
  long_strike: number
  short_strike: number
  quantity: number
  open_qty: number
  status: string
  entry_fill: number | null
  exit_deadline: string
}

export interface IdeasCardHistory {
  lines: string[]
  ledger_sha256_12: string
}

export interface IdeasResearchEntry {
  section: string
  line: string
}

export interface IdeasResearch {
  ledger_date: string
  sha256_12: string
  entries: IdeasResearchEntry[]
}

export interface IdeasProtocol {
  allowed_direction: string[]
  context_only: string[]
  advisory: boolean
}

export interface SymbolIdeas {
  now: string
  symbol: string
  signals: IdeasSignals | null
  queue: IdeasQueue | null
  paper_positions: IdeasPaperPosition[]
  cards: IdeasCardHistory | null
  research: IdeasResearch | null
  protocol: IdeasProtocol
}

export interface ScenarioStats {
  count: number
  wins: number
  win_rate: number
  mean_pnl: number
  median_pnl: number
  p10_pnl: number
  worst_pnl: number
  best_pnl: number
}

/** M4 valuation scenario — simulated, never counted in real stats. */
export interface ScenarioDoc {
  key: string
  generated_at: string
  label: string
  age_seconds: number | null
  error: string | null
  structure?: {
    underlying: string
    expiry: string
    short: number
    long: number
    dte: number
    debit_mid: number
    debit_ask: number | null
    spot_now: number
    found_in: string
  }
  assumptions?: Record<string, string | number | null>
  iv?: number
  iv_source?: string
  analogs?: ScenarioStats
  pessimistic?: ScenarioStats | null
  iv_band_mean_pnl?: { lo: number | null; hi: number | null }
  recent?: {
    entry_ms: number
    entry_spot: number
    entry_debit: number
    final_pnl: number
    series: HistorySeries
  }
  sessions?: number
}

// --- RL-1 (research lab) contracts: catalog + comparisons + evidence ---
// Handoff: ~/pop-deck-uploads/2026-09/TREX-Research-Lab-Design-and-Build-Handoff-29a9aa.md
// Mirror of src/tree_options/research/contracts.py — keep string values in lockstep.

export type ResearchEvidenceKind =
  | 'synthetic_backtest'   // backtest/equity.py synthetic/v1
  | 'shadow_proxy'         // desk/shadows.py EOD-deadline proxy
  | 'sealed_campaign'      // artifacts/campaign-2026-09/<scope>/sealed-round.json
  | 'paper_execution'      // DESK_PAPER_DIR; reserved (RL-2 only)
  | 'broker_paper'         // E5; explicit out-of-scope for RL-1

export type ResearchRegistration =
  | 'before_entry_window_end'
  | 'retrospective_backfill'

export type ResearchDisposition =
  | 'PASS'
  | 'FAIL'
  | 'HOLD-STANDS'
  | 'DATA-GATED-NOT-RUN'
  | 'NOT_EVALUABLE'
  | 'WITHDRAWN'
  | 'INSUFFICIENT_N'
  | 'INSUFFICIENT_COVERAGE'
  | 'NOT_CANDIDATE'
  | 'DESCRIPTIVE-ONLY:NO-REGIME-SIGNAL'
  | 'NOT_EVALUABLE-SEALED'

export interface ResearchCandidate {
  id: string
  family: string
  version: string
  evidence_kind: ResearchEvidenceKind
  registration: ResearchRegistration
  disposition: ResearchDisposition
  plot_funded_account: boolean
  supported_start: string | null
  supported_end: string | null
  artifact_hashes: Record<string, string>
  capabilities: string[]
  ineligibility_reason: string | null
  data_completeness: Record<string, unknown>
  warnings: string[]
  source_url: string
}

export interface ResearchCandidatesResponse {
  candidates: ResearchCandidate[]
}

export interface ResearchEvidenceEnvelope {
  candidate_id: string
  point_session: string | null
  hypothesis: string
  estimand: string
  exact_versions: Record<string, string>
  cohort_membership: string[]
  registered_or_exploratory: ResearchRegistration
  diagnostics: Record<string, unknown>
  robustness: string[]
  source_artifacts: { path: string; sha256: string }[]
  reproduction_command: string
  warnings: string[]
}

export interface ComparisonSpec {
  candidate_ids: string[]
  starting_capital: string                 // Decimal as string across the wire
  common_start: string | null
  common_end: string | null
  cashflow_timing: 'beginning_of_period' | 'end_of_period'
  contribution_per_period: string
  cost_model_kind: 'five_bp_fixed' | 'pass_through'
  benchmark_candidate_id: string | null
  currency: 'USD'
  price_basis: 'nominal_pretax'
  idle_cash_policy: 'cash_yields_zero'
  rebalancing: 'none' | 'monthly'
  position_sizing: 'integer' | 'fractional'
  collateral: 'none'
  borrowing: 'none'
  knowledge_cutoff: string | null           // ISO instant
  proposed_by: string
  notes: string
}

export interface ComparisonRunResponse {
  run_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  spec_hash: string
  workspace: string
}

export interface ComparisonRow {
  date: string
  ending_value: string | null
  starting_capital: string | null
  committed_signed: string | null
  contributions: string | null
  withdrawals: string | null
  gain: string | null
  idle_cash: string | null
  fees_paid: string | null
}

export interface DrawdownRow {
  date: string
  drawdown_dollar: string
  drawdown_pct: string
  recovery_end_date: string | null
}

export interface CandidateResultSummary {
  candidate_id: string
  candidate: ResearchCandidate
  rows_by_date: Record<string, ComparisonRow>
  drawdown: Record<string, DrawdownRow>
  fees_paid_total: string
  sample_size: number
  sample_floor: number
  sample_floor_met: boolean
  rejection_reason: string | null
  final_ending_value: string | null
}

export interface ComparisonResultResponse {
  spec: ComparisonSpec
  candidates: CandidateResultSummary[]
  paired_diff: Record<string, Record<string, { value: string | null; reason: string | null }>>
}

export interface ResearchErrorResponse {
  schema: 'research-error/1'
  error: string
  message: string
}
