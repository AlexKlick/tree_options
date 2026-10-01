// Mirrors the Python API contract (src/tree_options/trex_web/app.py).
export interface ActionNode {
  id: string
  label: string
  operation: string
  operation_version: string
  owner_role: string
  effect_class: string
  target_environment: string
  dependencies: { node_id: string; on_outcomes: string[] }[]
  inputs: Record<string, { artifact_id?: string; producer_node_id?: string; output_name?: string; expected_type: string }>
  outputs: Record<string, string>
  required_guards: string[]
  required_receipts: string[]
  postcondition: string
}

export interface ActionModelExample {
  plan: {
    plan_id: string
    revision: number
    goal: string
    state: 'proposed'
    artifact_status: 'synthetic_design_example'
    execution_authorized: false
    source_repository_head: string
    artifacts: { id: string; kind: string; sha256: string; classification: string }[]
    nodes: ActionNode[]
  }
  receipt: {
    valid_structure: boolean
    node_count: number
    dependency_count: number
    execution_authorized: false
    broker_contacted: false
    scope: string
  }
}

export interface HistoricalReplayList {
  schema: 'desk-historical-replay-list/1'
  execution_enabled: false
  reports: {
    id: string
    label: string
    spec: { start: string; end: string; names: string[]; entry_dte: number[]; haircut: number; max_loss: number }
    counts: Record<string, number>
    by_structure: Record<string, { trades: number; wins: number; win_rate: number | null; mean_pnl: number | null }>
    by_variant: Record<string, { trades: number; wins: number; win_rate: number | null; mean_pnl: number | null; worst_pnl: number | null }>
    eligibility_by_variant?: Record<string, Record<string, number>> | null
    provenance: { sources: { path: string; input_sha256: string; input_files: number }[] }
    limitations: string[]
  }[]
}

export interface PortfolioScenarioList {
  schema: 'desk-portfolio-scenario-list/1'
  execution_enabled: false
  reports: {
    id: string
    label: string
    spec: { intended_capital: string; max_trade_loss: string; max_open_loss: string }
    variants: Record<string, {
      considered: number; admitted: number
      skipped: { trade_cap: number; open_cap: number; capital: number }
      peak_open_loss_reserved: string; closed_pnl: string
      ending_closed_capital: string; minimum_closed_capital: string
    }>
    provenance: { replay_sha256: string; code_head: string; code_dirty: boolean }
    limitations: string[]
  }[]
}

export interface IntradayGraphList {
  schema: 'desk-intraday-graph-list/1'
  execution_enabled: false
  reports: {
    id: string
    policy: string
    source_sha256: string
    requested_contracts: number
    captured_contracts: number
    traded_minute_bars: number
    limitations: string[]
    windows: {
      start: string; end: string; sessions: number; scheduled_snapshots: number
      potential_trades: number; entered: number; modeled_wins: number; modeled_losses: number
      open_at_end: number; closed_capital_proxy: string
      minimum_closed_capital_proxy: string; peak_open_loss_reserved: string
    }[]
  }[]
}

export interface TradeFloorReplayList {
  schema: 'desk-trade-floor-list/1'
  execution_enabled: false
  replays: TradeFloorReplay[]
}

export interface TradeFloorReplay {
  schema: 'desk-trade-floor-replay/1'
  id: string
  source_head: string
  source_manifest_sha256: string
  sample_manifest_sha256: string
  provider_manifest_sha256: string
  replay_manifest_sha256: string
  starting_capital: string
  excluded_calibration_snapshot: string
  execution_enabled: false
  research_only: true
  limitations: string[]
  windows: {
    id: string; start: string; end: string; series: number
    traded_minute_bars: number; rounds: number
    final_scores: { id: string; label: string; entered: number; wins: number
      losses: number; closed_capital_proxy: string }[]
  }[]
  rounds: {
    id: string; window: string; snapshot_id: string; as_of: string
    all_as_of_candidates: number
    candidates: { id: string; symbol: string; structure: string; dte: number
      width: string; premium_proxy: string; max_loss_proxy: string
      max_gain_proxy: string; reward_to_risk_proxy: string }[]
    traders: { id: 'zai' | 'flash' | 'minimax'; label: string
      selected_id: string | null; action: 'skip' | 'blocked' | 'entered'
      action_reason: string; model_reason: string
      entry_loss_proxy: string | null; eventual_pnl_proxy: string | null }[]
  }[]
}

// The ACCOUNT's exposure as the desk's own book adapter reads it, split by
// owner. The desk book is the supervised desk's; every other book under the
// same state root belongs to the legacy trex monitor — a different service
// on the same paper account. A flat desk book is NOT a flat account.
// `countable` false: a book could not be read and no total may be claimed.
export interface AccountExposurePosition {
  id: string
  book: string
  owner: string
  underlying: string
  status: string
  quantity: number
  max_loss_usd: string | null
  exit_deadline: string | null
}

export interface AccountExposureSlice {
  structures: number
  legs: number | null
  max_loss_usd: string | null
  earliest_exit_deadline: string | null
  exit_deadlines_unknown: number
  owners: string[]
  books: string[]
  positions: AccountExposurePosition[]
}

export interface AccountExposure {
  schema: 'desk-account-exposure/1'
  as_of: string
  state_root: string | null
  desk_run_dir: string
  countable: boolean
  max_loss_usd: string | null
  outside_desk_book: AccountExposureSlice
  desk_book: AccountExposureSlice
  problems: string[]
}

// GET /api/desk/supervised: the desk CLI's files-only status dump. The
// desk process owns the broker session; this payload is what it left on
// disk (kill files, book, inbox, mandate, outbox, events) plus the
// account exposure beside the desk book.
export interface SupervisedDeskStatus {
  schema: 'desk-cli-status/1'
  at: string
  run_dir: string
  kill_files: string[]
  owner: Record<string, unknown> | null
  /** absent on an older backend: the panel then shows no account banner */
  account_exposure?: AccountExposure
  book: Record<string, { status: string | null; open_qty: number }> | null
  inbox: string[]
  last_results: {
    schema?: string
    at?: string
    request?: string
    intent_id?: string
    status: string
    reason?: string
    detail?: unknown
    blockers?: string[]
    permit_id?: string
  }[]
  supervised: {
    schema: 'supervised-status/1'
    at: string
    mandate: {
      state: 'active' | 'expired' | 'revoked' | 'absent'
      days_left?: number
      account_id?: string
      max_orders?: number
      orders_used?: number
      long_running?: boolean | null
      expires_at?: string
      [key: string]: unknown
    }
    outbox: { intent_id: string; state: string; [key: string]: unknown }[]
  }
  events?: Record<string, unknown>[]
}

// GET /api/desk/automation: timer settings + kill-file states for the
// desk's own user units (the whitelist is the whole surface).
export interface AutomationStatus {
  schema: 'desk-automation/1'
  kill_files: string[]
  timers: {
    key: string
    what: string
    timer: string
    service: string
    enabled: boolean
    active: boolean
    next_elapse: string
    last_result: string
    last_exit: string
  }[]
}

// GET /api/desk/lab: per-policy fold of the lab's run summaries plus the
// annotation-only advisory (promoted is false by construction).
export interface LabScoreboard {
  schema: 'desk-lab-scoreboard/1'
  execution_enabled: false
  policies: Record<string, LabPolicyStats>
  advisory: {
    policy: string
    stats: LabPolicyStats
    promoted: false
    basis: string
  } | null
}

export interface LabPolicyStats {
  runs: number
  boards: number
  model_calls: number
  model_failures: number
  entered: number
  modeled_wins: number
  modeled_losses: number
  closed_pnl_sum: string
  worst_minimum_capital: string | null
  last_run: string
  kinds: string[]
}

// GET /api/desk/longrun: the latest desk long run — live progress, plus the
// digest's standings once finished. Every total carries its session-bootstrap
// 95% CI; promoted is false by construction (the route refuses otherwise).
export interface LongRunPaired {
  diff_total: number
  ci95: [number, number]
  p_one_sided: number
  sessions: number
}

export interface LongRunArmProgress {
  policy: string
  repeat: number
  kind: string
  done: number | null
  total: number | null
  entered: number | null
  failures: number | null
  unevaluable: number | null
  net: number | null
}

export interface LongRunProgress {
  schema: 'desk-longrun-progress/1'
  legacy?: string
  status: string
  at: string | null
  started: string | null
  boards: number | null
  sessions: number | null
  total: number | null
  finished: number | null
  failures: number | null
  paused_s: number | null
  quota: { ok: boolean | null; reason: string; checked_at: string | null } | null
  calls_per_s: number | null
  eta_s: number | null
  arms: Record<string, LongRunArmProgress>
  digest: string | null
}

export interface LongRunStanding {
  arm: string
  policy: string
  repeat: number
  kind: string
  boards: number
  entered: number
  entry_rate: number | null
  unevaluable: number
  failures: number
  net_total: number
  net_ci95: [number, number]
  vs_random: LongRunPaired
  vs_random_own?: LongRunPaired & { p_enter: number }
  null_percentile_own?: number
  vs_first_row: LongRunPaired | null
  vs_incumbent: LongRunPaired | null
  // vs the always_bullish regime baseline (null for that arm itself)
  vs_regime?: LongRunPaired | null
  null_percentile: number
}

export interface LongRunBenchmark {
  name: string
  status: 'ok' | 'unavailable'
  net_total?: number
  net_ci95?: [number, number]
  base_date?: string
  missing_sessions?: number
  reason?: string
}

export interface LongRunFinalist {
  policy: string
  holm_p: number
  test: {
    net_total: number
    net_ci95: [number, number]
    vs_random: LongRunPaired
    vs_incumbent: LongRunPaired | null
  }
  eligible_for_operator_review: boolean
  test_entries?: number
  rule_check?: Record<string, boolean>
}

export interface LongRunDigest {
  assessment_class?: 'registered_protocol' | 'retrospective_descriptive'
  pricing_status?: 'DATA_GATED' | 'PRICED_SIMULATION'
  cost_model?: string
  cost_provenance?: Record<string, unknown>
  no_price?: { total: number; by_arm: Record<string, number>; by_reason: Record<string, number> }
  headline: string
  untrusted_note: string
  evaluation_valid: boolean
  complete: boolean
  at: string
  promotion: { promoted: false; rule: string }
  boards: {
    total: number
    scored: number
    excluded: number
    sessions: { count: number; first: string | null; last: string | null }
  }
  aa: {
    status: 'valid' | 'INVALID' | 'not_run'
    valid: boolean
    rule: string
    pair?: string[]
    boards?: number
    agreement?: number
    diff?: LongRunPaired
    reason?: string
  }
  random_null: {
    p_enter: number
    matched_to: string[]
    horizons: (string | null)[]
    seeds: number
    expected_total: number
    expected_ci95: [number, number]
    simulated_mean_total: number
    band95: [number, number]
  }
  standings: LongRunStanding[]
  walk_forward: {
    alpha?: number
    scoring_version?: string
    null_scope?: string
    min_test_entries?: number
    entry_count_unit?: string
    assessment_class?: 'registered_protocol' | 'retrospective_descriptive'
    status: string
    cutoff?: string | null
    metric?: string | null
    max_finalists?: number | null
    tune_sessions?: number | null
    test_sessions?: number | null
    reason?: string | null
    finalists: LongRunFinalist[]
  }
  benchmarks: LongRunBenchmark[]
}

export interface LongRunView {
  schema: 'desk-longrun-view/1'
  run: string | null
  progress: LongRunProgress | null
  digest: LongRunDigest | null
  execution_enabled: false
}

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
  funded_history: 'reconstructed' | 'unavailable' // DATA support, independent of verdict
  funded_history_reason: string | null
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
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'blocked'
  spec_hash: string
  workspace: string
}

export interface ComparisonRow {
  cash: string
  inventory: [string, number][]            // symbols with nonzero holdings
  marked_value: string | null              // null = a held symbol lacks a mark
  nav: string | null                       // account value; null = gap, never zero
  contributions_cum: string
  withdrawals_cum: string
  fees_cum: string
  realized_pnl_cum: string
  investment_gain: string | null           // NAV − opening − contributions + withdrawals
  missing_mark_symbols: string[]
}

export interface DrawdownRow {
  drawdown_dollar: string
  drawdown_pct: string
  recovery_end_date: string | null
}

export interface CandidateResultSummary {
  candidate_id: string
  candidate: ResearchCandidate
  rows_by_date: Record<string, ComparisonRow> // ISO date keys (to_wire boundary)
  drawdown: Record<string, DrawdownRow>
  fees_paid_total: string
  excluded_out_of_window: number
  sample_size: number
  sample_floor: number
  sample_floor_met: boolean
  rejection_reason: string | null
  final_ending_value: string | null
}

export interface ComparisonResultWire {
  spec: ComparisonSpec
  rejection: string | null
  candidates: CandidateResultSummary[]
  paired_diff: Record<string, Record<string, { value: string | null; reason: string | null }>>
}

/** GET /runs/{id}/result — the lifecycle envelope. The result payload
 * is present exactly when status === 'completed'; GET never computes. */
export interface RunResultResponse {
  run_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'blocked'
  result: ComparisonResultWire | null
  error?: string | null
  result_sha256?: string
  engine_sha256?: string
  input_snapshot_sha256?: string
  calendar_sha256?: string
}

export interface ResearchErrorResponse {
  schema: 'research-error/1'
  error: string
  message: string
}

// --- RL-3 (forecast lane): calibrated outlook — evaluation receipts ---
// Mirror of src/tree_options/research/forecast/{contracts,engine,sources}.py
// and the GET/POST /api/research/forecast routes in
// trex_web/research_view.py — keep field names and string values in
// lockstep. Numbers on this surface are native JSON FLOATS (statistics
// and index levels, never money), so they stay `number`, not string.

/** POST body == the stored spec; the run id hashes exactly this. */
export interface ForecastSpec {
  source: string                     // ForecastSourceId.value, e.g. 'index:VIX'
  horizon: number
  evaluation_start: string           // ISO date
  evaluation_end: string | null
  proposed_by: string
  notes: string
}

/** A listed-but-disabled horizon (63/126): shown, never selectable. */
export interface ForecastIllustrativeHorizon {
  horizon: number
  enabled: false
  status: 'illustrative_only'
  status_copy: string
}

/** An enabled horizon plus its freshness-qualified latest receipt. */
export interface ForecastEnabledHorizon {
  horizon: number
  enabled: true
  latest_receipt_run_id: string | null
  receipt_series_sha256: string | null
  current_series_sha256: string | null
  receipt_engine_sha256: string | null
  current_engine_sha256: string | null
  receipt_calendar_sha256: string | null
  current_calendar_sha256: string | null
  receipt_session_authority_sha256: string | null
  current_session_authority_sha256: string | null
  /** fresh only when EVERY binding matches the current world; null
   * when no receipt exists at all. */
  fresh: boolean | null
  last_attempt_refused?: { code: string; n_evaluated: number | null }
}

export type ForecastHorizonStatus =
  | ForecastEnabledHorizon
  | ForecastIllustrativeHorizon

export interface ForecastSourceStatus {
  source: string
  label: string
  basis: string
  grid_basis: string
  horizons: ForecastHorizonStatus[]
}

export interface ForecastMetadata {
  schema: 'research-forecast-metadata/1'
  quantile_grid: number[]
  origin_floor: number
  min_history_sessions: number
  paired_floor: number
  interval_semantics: string
  sources: ForecastSourceStatus[]
}

/** One origin's full trace for one model (contracts.LedgerRow). */
export interface ForecastLedgerRow {
  origin_date: string
  target_date: string | null          // null: target beyond the data
  training_count: number
  status: 'evaluated' | 'failed' | 'excluded'
  reason: string | null
  actual: number | null
  quantiles: number[]
  losses_by_tau: number[]
}

/** contracts.OriginTally; the RESULT wire headline adds failed_by_model. */
export interface ForecastOriginsTally {
  total: number
  evaluated: number
  excluded: number
  excluded_reasons: Record<string, number>
  floor: number
  floor_met: boolean
  failed_by_model?: Record<string, number>
}

export interface ForecastSeriesSummary {
  n_sessions: number
  first_session: string | null
  last_session: string | null
  series_sha256: string
  basis: string
  grid_basis: string
  provenance: Record<string, unknown>
  excluded_rows: Record<string, string[]>
  n_source_rows: number
}

export interface ForecastCoverage90 {
  hits: number
  n: number                            // coverage is never shown without n
  point: number | null
  wilson_low: number | null
  wilson_high: number | null
  wilson_note: string
  bootstrap_low: number | null
  bootstrap_high: number | null
  bootstrap_block: number
  bootstrap_seed: number
  bootstrap_reason?: string            // present when the bootstrap degenerated
}

export interface ForecastDmBlock {
  n: number
  mean: number
  stat: number
  p_one_sided: number
  lag: number
  lag_units: string                    // 'origin_index'
  direction: string                    // 'baseline loss - model loss'
  series_note: string
  sensitivity: Record<string, { stat: number; p_one_sided: number } | null>
}

export interface ForecastSkill {
  baseline: string
  paired_n: number
  loss_paired: number | null
  bench_paired: number | null
  pinball_skill: number | null
  reason: string | null                // e.g. paired_cohort_insufficient
  dm: ForecastDmBlock | null
  dm_unavailable_reason?: string
}

/** The honest fallback when finite per-origin losses overflow an
 * aggregate: metrics withheld with a reason, never non-finite floats. */
export interface ForecastDegradedMetrics {
  aggregate_status: 'non_finite'
  reason: string
  n_evaluated: number
}

export interface ForecastModelMetrics {
  pinball_by_tau: Record<string, number>
  grid_quantile_score: number          // a GRID score; never named CRPS
  coverage_90: ForecastCoverage90
  mean_width_90: number
  skill_vs_baseline: ForecastSkill | null   // null: this model IS the baseline
}

export interface ForecastModelReceipt {
  model: string
  is_baseline: boolean
  n_evaluated: number
  n_failed: number
  failure_reasons: Record<string, number>
  metrics: ForecastModelMetrics | ForecastDegradedMetrics
  ledger: ForecastLedgerRow[]
}

export interface ForecastFanEntry {
  model: string
  status: 'ok' | 'unavailable'         // a latest-fit failure is explicit
  quantiles?: Record<string, number>   // '0.05'..'0.95' -> level; ok only
}

export interface ForecastForward {
  origin_session: string
  last_close: number
  horizon_sessions: number
  beyond_data: boolean
  target_session: string | null        // null: target lies beyond the data
  fan: ForecastFanEntry[]
}

export interface ForecastStudyBlock {
  schema: 'research-forecast-study/1'
  estimand: string
  target: string
  data_vintage: Record<string, unknown>
  windows: { evaluation_start: string; evaluation_end: string | null }
  models: string[]
  benchmark: string | null
  primary_score: string
  inference: Record<string, unknown>
  model_notes: Record<string, string>
  access_mode: string                  // 'exploratory' in v1
}

/** research-forecast-result/1 — the evaluation receipt. */
export interface ForecastResultWire {
  schema: 'research-forecast-result/1'
  source: string
  source_basis: string
  grid_basis: string
  horizon: number
  quantile_grid: number[]
  quantile_interpolation: string
  series: ForecastSeriesSummary
  evaluation_window: { start: string; end: string | null }
  study: ForecastStudyBlock
  execution_status: string
  evaluation_status: string            // 'receipt_published' | 'below_floor'
  calibration_status: string           // always 'not_claimed' in v1
  origins: ForecastOriginsTally
  models: ForecastModelReceipt[]
  forward: ForecastForward
  refusal: null
}

/** research-forecast-refusal/1 — a typed refusal is a recorded outcome
 * with the evidence retained, never a 404. The payload varies by code
 * (drift refusals carry both shas; the floor refusal carries the tally
 * and every model's ledger). */
export interface ForecastRefusalWire {
  schema?: 'research-forecast-refusal/1'
  refusal: string
  message: string
  source?: string
  horizon?: number
  origins?: ForecastOriginsTally
  grid_reasons?: Record<string, number>
  models?: Array<{ model: string; tally: ForecastOriginsTally; ledger: ForecastLedgerRow[] }>
  [key: string]: unknown
}

/** GET /api/research/runs/{id}/result narrowed to forecast runs: the
 * lifecycle envelope with the forecast result/refusal wire inside. */
export interface ForecastRunResultResponse {
  run_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'blocked'
  result: ForecastResultWire | ForecastRefusalWire | null
  error?: string | null
  result_sha256?: string
  engine_sha256?: string
  input_snapshot_sha256?: string
  calendar_sha256?: string
  session_authority_sha256?: string
}

/** GET /api/research/runs/{id} narrowed to forecast runs (the stored
 * run record carries the submission-time bindings). */
export interface ForecastRunResponse {
  run_id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'blocked'
  spec_hash: string
  kind: 'forecast'
  [key: string]: unknown
}

export interface QuantTheoryMetrics {
  evidence_kind: 'BACKTEST'
  capital_policy: 'independent_equal_capital_roundtrips'
  disposition: 'SCORED' | 'INCOMPLETE'
  period_count: number
  scored_period_count: number
  compound_nav: null
  max_drawdown_scope: 'endpoint_loss_only'
  mean_net_return: string | null
  max_drawdown: string | null
  turnover: string | null
  fees: string | null
  execution_authorized: false
  exact_external_economics: false
}

export interface QuantTheoryNode {
  schema: 'quant-research-node/1'
  node_id: string
  campaign_id: string
  stage: string
  payload_sha256: string
  payload_ref?: string
  parents: string[]
  execution_authorized: false
}

export interface QuantTheoryCampaign {
  schema: 'quant-theory-result/1'
  campaign_id: string
  hypothesis: string
  data_class: 'synthetic_fixture' | 'user_supplied_unqualified'
  evidence_kind: 'synthetic_backtest' | 'simulated_execution'
  registration: 'exploratory_retrospective'
  candidate_count: number
  reflection_calls: number
  winner: {strategy_id: string; parameters: {top_n?: number}; version_id: string}
  holdout: {candidate: QuantTheoryMetrics; control: QuantTheoryMetrics}
  graph: QuantTheoryNode[]
  disposition: 'REVIEW_REQUIRED' | 'HOLDOUT_INCOMPLETE'
  objective: string
  limitations: string[]
  execution_authorized: false
  exact_external_economics: false
  live_money: false
}

export interface QuantLabProjection {
  strategies: {strategy_id: string; version: string; registration: string; data_status: string; description: string; required_inputs: string[]}[]
  versions: {version_id: string; config_sha256: string; code_sha: string; lock_sha256: string}[]
  experiments: {run_id: string; strategy_version: string; disposition: string; evidence_kind: string; knowledge_cutoff: string; universe: {as_of: string; members: string[]}; scores: {entity_id: string; score: string; rank: number}[]; targets: {entity_id: string; weight: string}[]; exclusions: Record<string, string>; evidence: {exact_versions: Record<string, string>}}[]
  comparisons: {candidate_run: string; control_run: string; common_snapshot: string}[]
  campaigns?: Record<string, unknown>[]
  theory_campaigns?: QuantTheoryCampaign[]
  evidence_classes: string[]
  execution: {state: string; environment: string; provenance?: {operation: string; intent_id: string; refs: Record<string, unknown>}[]; risk?: Record<string, unknown>; account_alias?: string; owner_epoch?: string; observed_at?: string; mandate?: {state: string}; executions?: {intent_id: string; state: string; broker_state: string; reconciliation_clean: boolean; findings: string[]; evidence_verdict: string; exact_economics: boolean; records: Record<string, unknown>[]}[]}
  live_money: false
  execution_authorized: false
}
