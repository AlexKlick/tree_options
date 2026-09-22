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
}

export interface PlansResponse {
  now: string
  gateway_reachable: boolean
  plans: PlanSummary[]
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
  last: { ts_ms: number; pnl: number; pos: boolean }
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
