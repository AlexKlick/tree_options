// TREX Wave 3/4 frozen contracts, TypeScript mirror (docs/product/WAVE3-CONTRACTS.md).
//
// Python is the source of truth: src/tree_options/trex/contracts/wave3.py and
// acceptance.py (Wave 3), contracts/commands.py and the trex_web payload
// functions (Wave 2). The shared JSON fixtures under
// tests/contracts/fixtures/wave3/ pin both sides: Python round-trips them
// through the models, contracts.test.ts checks every key list below against
// them. Change a shape here only together with its Python model.
//
// The null rule: a null is UNKNOWN (its block says `unavailable` with an
// `unavailable_reason`, or a sibling `*_unavailable_reason` names why) or
// NOT APPLICABLE (fixed by a sibling enum, e.g. `since` only while a kill file
// stands). Zero, "" and [] are values, never stand-ins for unknown. The block
// types below encode this: narrow on `status` and the fields are non-null.

// ---------------------------------------------------------------- primitives

/** A UTC instant, ISO 8601 with `Z` (`2026-10-22T14:02:00Z`). */
export type Instant = string
/** An ISO calendar date (`2026-10-22`); sessions are America/New_York dates. */
export type IsoDate = string
/** 64 lower-case hex characters. */
export type Sha256Hex = string
/** A lower-case canonical UUID; also the command's file name. */
export type IdempotencyKey = string
export type AssetKind = 'option' | 'equity'

// ---------------------------------------------------------------- vocabulary

/** `Wave3Reason` (wave3.py), in declaration order; disjoint from Wave 2 `Blocker`. */
export const WAVE3_REASONS = [
  'calendar_unreadable',
  'calendar_exhausted',
  'heartbeat_absent',
  'runtime_lock_unknown',
  'gateway_watch_not_reporting',
  'gateway_watch_stale',
  'kill_file_unreadable',
  'proposals_dir_unreadable',
  'acceptance_missing',
  'acceptance_invalid',
  'acceptance_calendar_mismatch',
  'acceptance_window_mismatch',
  'schedule_unavailable',
  'activity_source_unreadable',
  'actor_not_recorded',
  'desk_owner_stopped',
  'desk_owner_stale',
  'gateway_needs_login',
  'gateway_needs_2fa',
  'gateway_starting',
  'gateway_api_down',
  'gateway_down',
  'exit_unguarded',
  'calendar_horizon_near',
  'family_commands_disabled',
  // Wave 3/4 integration amendment (additive)
  'acceptance_owners_mismatch',
] as const
export type Wave3Reason = (typeof WAVE3_REASONS)[number]
/** A Wave 2 `Blocker` value (contracts/vocabulary.py; includes view reasons
 * such as `holdings_missing`) or a `Wave3Reason`. The server validates it. */
export type ReasonCode = Wave3Reason | (string & {})

export const DRILL_IDS = [
  'relogin',
  'restart_mid_position',
  'gateway_outage',
  'duplicate_command',
  'backup_restore',
  'notification',
  'unassisted_use',
] as const
export type DrillId = (typeof DRILL_IDS)[number]

export const ACTIVITY_SOURCES = ['command', 'desk_event', 'audit', 'drill'] as const
export type ActivitySource = (typeof ACTIVITY_SOURCES)[number]

export const PILOT_ROLES = ['observation', 'baseline', 'challenger'] as const
export type PilotRole = (typeof PILOT_ROLES)[number]

export const PILOT_BUDGET_CEILINGS = {
  compute_minutes_per_day: 120,
  requests_per_day: 5000,
  storage_mb: 2048,
} as const

export const ACCEPTANCE_THRESHOLDS = {
  heartbeat_min_percent: 99,
  lifecycles_per_kind: 10,
  max_exit_unguarded_s: 300,
  open_check_window_s: 900,
  streak_sessions: 10,
} as const

// ---------------------------------------------------------------- API paths

/** Relative to the app's mount (`/` on loopback, `/trex/` through the portal). */
export const API_PATHS = {
  today: '/api/today',
  activity: '/api/activity',
  acceptance: '/api/acceptance',
  commands: '/api/commands',
  proposals: '/api/proposals',
  portfolio: '/api/portfolio',
} as const

// ---------------------------------------------------------------- controls

export const PRIMARY_CONTROL = 'pause_entries' as const
/** Owner-facing labels per command kind. `halt`'s wording is the plan's. */
export const CONTROL_LABELS = {
  pause_entries: 'Pause new entries',
  resume_entries: 'Resume new entries',
  halt: 'Emergency stop (also stops exits)',
  flatten: 'Close all desk positions',
  resume_all: 'Clear emergency stop, close-all and pause',
} as const

// ---------------------------------------------------------------- blocks

/** A block that is known (`status` one of `S`, every field non-null) or
 * unavailable (`unavailable_reason` set, every field null). */
export type Block<S extends string, F> =
  | ({ status: S; unavailable_reason: null } & F)
  | ({ status: 'unavailable'; unavailable_reason: ReasonCode } & { [K in keyof F]: null })

export function isKnown<B extends { status: string }>(
  block: B,
): block is Exclude<B, { status: 'unavailable' }> {
  return block.status !== 'unavailable'
}

// ---------------------------------------------------------------- trex-today/1

export type SessionState = Block<
  'pre_open' | 'open' | 'after_close' | 'no_session',
  {
    et_date: IsoDate
    session: IsoDate
    opens_at: Instant
    closes_at: Instant
    early_close: boolean
    calendar_sessions_left: number
  }
>

export type DeskOwnerHealth = Block<
  'healthy' | 'stale' | 'stopped',
  {
    heartbeat_at: Instant
    heartbeat_age_s: number
    lock_held: boolean
    exit_owner_ready: boolean
  }
> & { stale_after_s: number }

export type GatewayStatus =
  | 'ok'
  | 'starting'
  | 'checking'
  | 'needs_login'
  | 'needs_2fa'
  | 'api_down'
  | 'down'
export type GatewayHealth = Block<GatewayStatus, { detail: string; checked_at: Instant; age_s: number }>

export type KillFile = 'PAUSE_ENTRIES' | 'HALT' | 'FLATTEN'
export type KillSwitchState = { name: KillFile } & (
  | { status: 'clear'; unavailable_reason: null; since: null; reason: null }
  | { status: 'standing'; unavailable_reason: null; since: Instant; reason: string }
  | { status: 'unavailable'; unavailable_reason: ReasonCode; since: null; reason: null }
)

export interface QueuedCommand {
  idempotency_key: IdempotencyKey
  kind: CommandKind
  issued_at: Instant
  expires_at: Instant
  issued_by_sub: string
}
export type QueuedCommands = Block<'ok', { items: QueuedCommand[] }>

export interface Controls {
  pause_entries: KillSwitchState
  halt: KillSwitchState
  flatten: KillSwitchState
  queued: QueuedCommands
}

export interface PendingApproval {
  proposal_id: Sha256Hex
  proposal_sha256: Sha256Hex
  asset_kind: AssetKind
  intent_id: string
  program_id: string
  summary: string
  proposed_at: Instant
  send_deadline: Instant
  /** `send_deadline - now` at the response's `now`; see `secondsLeft`. */
  seconds_to_deadline: number
  max_loss_cents: number
  notional_cents: number
  /** Copy verbatim into an `approve_entry` command's `args`. */
  approve_args: ApproveEntryArgs
  precheck_blockers: ReasonCode[]
}
export type PendingApprovals = Block<'ok', { items: PendingApproval[]; problem_count: number }>

export type HoldingsSummary = Block<
  'ok',
  { as_of: Instant; age_s: number; option_positions: number; equity_positions: number }
>
export type RiskSummary = Block<
  'fresh' | 'stale',
  {
    as_of: Instant
    age_s: number
    open_loss_cents: number
    day_realized_loss_cents: number
    settled_cash_cents: number
    outside_positions: number
  }
>
export interface PositionsSummary {
  holdings: HoldingsSummary
  risk: RiskSummary
}

export type AcceptanceSummary = Block<
  'ok',
  {
    computed_at: Instant
    verdict: 'met' | 'not_met'
    streak_sessions: number
    option_lifecycles: number
    equity_lifecycles: number
    drills_passed: number
  }
> & { streak_required: 10; lifecycles_required: 10; drills_required: 7; live_authorized: false }

export type SubjectEventKind = 'forced_exit' | 'exit_deadline' | 'approval_deadline'
export type GlobalEventKind =
  | 'session_open'
  | 'session_close'
  | 'entry_window_open'
  | 'entry_window_close'
  | 'premarket_run'
  | 'backup'
  | 'calendar_horizon'
export type ScheduledEvent =
  | { at: Instant; kind: SubjectEventKind; label: string; subject: string }
  | { at: Instant; kind: GlobalEventKind; label: string; subject: null }
export type NextEvents = Block<'ok', { items: ScheduledEvent[] }>

export interface HumanBlocker {
  code: ReasonCode
  scope: 'entries' | 'exits' | 'approvals' | 'commands' | 'display'
  severity: 'info' | 'warning' | 'critical'
  message: string
  next_step: string
}

export interface TodayView {
  schema: 'trex-today/1'
  now: Instant
  session: SessionState
  desk_owner: DeskOwnerHealth
  gateway: GatewayHealth
  controls: Controls
  approvals: PendingApprovals
  positions: PositionsSummary
  acceptance: AcceptanceSummary
  next_events: NextEvents
  blockers: HumanBlocker[]
  execution_enabled: false
}

/** Seconds left on an approval, counted from when the response ARRIVED
 * (never the client clock against `send_deadline`). Integration amendment
 * (lane F, additive): any value carrying `seconds_to_deadline` (a Today
 * `PendingApproval` or the facts built from a `GET /api/proposals` row). */
export function secondsLeft(
  approval: Pick<PendingApproval, 'seconds_to_deadline'>,
  receivedAtMs: number,
  nowMs: number,
): number {
  const elapsed = Math.floor(Math.max(0, nowMs - receivedAtMs) / 1000)
  return Math.max(0, approval.seconds_to_deadline - elapsed)
}

// ---------------------------------------------------------------- trex-activity/1

export type CommandStatus = 'accepted' | 'refused' | 'applied' | 'failed' | 'duplicate'
export type ActivityRefKey = 'proposal_id' | 'intent_id' | 'program_id' | 'drill_id' | 'path'

export interface ActivityActor {
  sub: string
  ingress: 'family' | 'loopback' | 'system'
}

export type ActivityItem = {
  /** `<source>:<source-local id>`, unique on a page. */
  id: string
  at: Instant
  kind: string
  summary: string
  refs: Partial<Record<ActivityRefKey, string>>
} & (
  | { actor: ActivityActor; actor_unavailable_reason: null }
  | { actor: null; actor_unavailable_reason: string }
) &
  (
    | { source: 'command'; command_key: IdempotencyKey; command_status: 'accepted' | 'applied'; reason: null }
    | {
        source: 'command'
        command_key: IdempotencyKey
        command_status: 'refused' | 'failed' | 'duplicate'
        reason: string
      }
    | { source: 'desk_event' | 'audit' | 'drill'; command_key: null; command_status: null; reason: null }
  )

export type ActivitySourceStatus = { source: ActivitySource } & (
  | { status: 'ok'; unavailable_reason: null }
  | { status: 'unavailable'; unavailable_reason: ReasonCode }
)

export interface ActivityPage {
  schema: 'trex-activity/1'
  now: Instant
  limit: number
  /** Newest first. */
  items: ActivityItem[]
  has_more: boolean
  /** Opaque; set exactly when `has_more`. Pass back as `?cursor=`. */
  next_cursor: string | null
  sources: ActivitySourceStatus[]
  execution_enabled: false
}

/** `GET /api/activity?limit=&cursor=&source=` (source repeatable). */
export function activityQuery(q: { limit?: number; cursor?: string | null; sources?: ActivitySource[] }): string {
  const params = new URLSearchParams()
  if (q.limit !== undefined) params.set('limit', String(q.limit))
  if (q.cursor) params.set('cursor', q.cursor)
  for (const source of q.sources ?? []) params.append('source', source)
  const text = params.toString()
  return text ? `${API_PATHS.activity}?${text}` : API_PATHS.activity
}

// ---------------------------------------------------------------- trex-acceptance/1

export interface EvidenceRef {
  path: string
  sha256: Sha256Hex
}
export type ReconciliationStatus = 'clean' | 'reconciliation_required' | 'unavailable'
export interface ReconciliationEpisode {
  started_at: Instant
  status: 'reconciliation_required' | 'unavailable'
  resolved_at: Instant | null
}
export type SessionMetric =
  | 'heartbeat_minutes'
  | 'reconciliation_open'
  | 'reconciliation_close'
  | 'reconciliation_episodes'
  | 'unowned_orders'
  | 'max_exit_unguarded_s'
export interface SessionRecord {
  session: IsoDate
  heartbeat_minutes: number | null
  reconciliation_open: ReconciliationStatus | null
  reconciliation_close: ReconciliationStatus | null
  reconciliation_episodes: ReconciliationEpisode[] | null
  unowned_orders: number | null
  max_exit_unguarded_s: number | null
  /** Names exactly the null metrics. */
  unavailable: Partial<Record<SessionMetric, string>>
  evidence: EvidenceRef[]
}
export interface LifecycleRecord {
  intent_id: string
  asset_kind: AssetKind
  proposal_id: Sha256Hex
  approval_key: IdempotencyKey
  approved_at: Instant
  filled_quantity: number
  first_fill_at: Instant | null
  exit_by: 'policy' | 'flatten' | 'manual' | 'not_exited'
  exited_at: Instant | null
  broker_flat_at: Instant | null
  reconciled_at: Instant | null
  commissions_complete: boolean | null
  commissions_unavailable_reason: string | null
  evidence: EvidenceRef[]
}
export interface DrillRecord {
  id: DrillId
  status: 'not_run' | 'passed' | 'failed'
  evidence: EvidenceRef[]
  attested_by: string | null
  at: Instant | null
  note: string
}
export interface CalendarSession {
  session: IsoDate
  opens_at: Instant
  closes_at: Instant
}
export interface SessionResult {
  session: IsoDate
  recorded: boolean
  qualifying: boolean
  reasons: string[]
}
export interface LifecycleResult {
  intent_id: string
  asset_kind: AssetKind
  completed: boolean
  counted: boolean
  reasons: string[]
}
export interface DrillResult {
  id: DrillId
  passed: boolean
  reasons: string[]
}
export interface StreakSummary {
  current_sessions: number
  current_start: IsoDate | null
  current_end: IsoDate | null
  best_sessions: number
  required: 10
}
export interface KindCounters {
  completed_total: number
  counted: number
  required: 10
}
export interface AcceptanceDocument {
  schema: 'trex-acceptance/1'
  computed_at: Instant
  calendar_sha256: Sha256Hex
  owners: string[]
  calendar_window: CalendarSession[]
  sessions: SessionRecord[]
  lifecycles: LifecycleRecord[]
  drills: DrillRecord[]
  session_results: SessionResult[]
  lifecycle_results: LifecycleResult[]
  drill_results: DrillResult[]
  streak: StreakSummary
  per_kind: { option: KindCounters; equity: KindCounters }
  drills_passed: number
  drills_required: 7
  verdict: 'met' | 'not_met'
  verdict_reasons: string[]
  /** Nothing in acceptance unlocks live trading. */
  live_authorized: false
}
export type AcceptanceView = {
  schema: 'trex-acceptance-view/1'
  now: Instant
  live_authorized: false
} & (
  | { status: 'ok'; unavailable_reason: null; document: AcceptanceDocument }
  | { status: 'unavailable'; unavailable_reason: ReasonCode; document: null }
)

// ---------------------------------------------------------------- trex-pilot-registration/1

export interface PilotRegistrationBody {
  schema: 'trex-pilot-registration/1'
  program_id: string
  role: PilotRole
  title: string
  hypothesis: string
  universe: { asset_kind: AssetKind; symbols: string[] }
  data_sources: {
    name: string
    provider: string
    dataset: string
    provenance: string
    license: string
    realtime: boolean
  }[]
  decision_rule: { statement: string; metric: string; minimum_sessions: number; minimum_lifecycles: number }
  controls: { program_id: string; relation: 'baseline' | 'observation' }[]
  budgets: { requests_per_day: number; compute_minutes_per_day: number; storage_mb: number }
  /** Set exactly for the observation role (cites the sealed QSL registration). */
  existing_registration: EvidenceRef | null
  created_at: Instant
}
export type PilotRegistration = PilotRegistrationBody &
  (
    | { status: 'draft'; sealed_by: null; sealed_at: null; sha256: null }
    | { status: 'sealed'; sealed_by: string; sealed_at: Instant; sha256: Sha256Hex }
  )
export interface BudgetUsage {
  schema: 'trex-pilot-usage/1'
  program_id: string
  day: IsoDate
  requests: number
  compute_seconds: number
  storage_bytes: number
  as_of: Instant
}

// ---------------------------------------------------------------- Wave 2 mirrors

// Shapes Wave 2 serves today, mirrored (not redefined): pinned by the fixtures
// wave3/wave2_*.json, which a Python test regenerates from the Wave 2 code.

export interface ApproveEntryArgs {
  proposal_id: Sha256Hex
  proposal_sha256: Sha256Hex
  effect_sha256: Sha256Hex
  owner_epoch: string
  profile_digest: Sha256Hex
}
export interface CommandArgs {
  /** Wave 5 amendment: `manual_equity_ticket` is set exactly for a
   * `manual:<uuid>` candidate (asset_kind `equity`); absent otherwise. */
  compose_entry: {
    program_id: string
    candidate_id: string
    asset_kind: AssetKind
    sleeve_ref?: string | null
    manual_equity_ticket?: ManualEquityTicket
  }
  approve_entry: ApproveEntryArgs
  reject_entry: { proposal_id: Sha256Hex; reason?: string }
  pause_entries: { reason?: string }
  resume_entries: { reason?: string }
  halt: { reason?: string }
  flatten: { reason?: string }
  resume_all: { reason?: string }
  backup_state: { label?: string | null }
  /** Wave 4 amendment (C3.5): seal a pilot registration; the hash the owner reviewed. */
  seal_registration: { program_id: string; registration_sha256: Sha256Hex }
}
export type CommandKind = keyof CommandArgs
/** The POST /api/commands body (`trex-command/1` without `issued_by`, which
 * the backend stamps; a body that carries it is refused). */
export type CommandBody<K extends CommandKind = CommandKind> = {
  [P in K]: {
    schema: 'trex-command/1'
    idempotency_key: IdempotencyKey
    kind: P
    args: CommandArgs[P]
    issued_at: Instant
    expires_at: Instant
  }
}[K]
export const COMMAND_MAX_TTL_S = 300

function instant(at: Date): Instant {
  return at.toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** A command body issued at `issuedAt` (whole seconds), living `ttlS` <= 300 s. */
export function buildCommandBody<K extends CommandKind>(
  kind: K,
  args: CommandArgs[K],
  idempotencyKey: IdempotencyKey,
  issuedAt: Date,
  ttlS: number = COMMAND_MAX_TTL_S,
): CommandBody<K> {
  if (!Number.isInteger(ttlS) || ttlS < 1 || ttlS > COMMAND_MAX_TTL_S) {
    throw new RangeError(`a command lives 1..${COMMAND_MAX_TTL_S} s`)
  }
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(idempotencyKey)) {
    throw new RangeError('idempotency_key is a lower-case UUID')
  }
  const issued = new Date(Math.floor(issuedAt.getTime() / 1000) * 1000)
  return {
    schema: 'trex-command/1',
    idempotency_key: idempotencyKey,
    kind,
    args,
    issued_at: instant(issued),
    expires_at: instant(new Date(issued.getTime() + ttlS * 1000)),
  } as CommandBody<K>
}

export interface CommandResult {
  schema: 'trex-command-result/1'
  idempotency_key: IdempotencyKey
  kind: CommandKind
  command_body_sha256: Sha256Hex
  status: CommandStatus
  reason: string | null
  detail: string
  result: Record<string, unknown> | null
  at: Instant
}

export interface EntryProposalDoc {
  schema: 'trex-entry-proposal/1'
  proposal_id: Sha256Hex
  effect: { schema: string; intent_id: string } & Record<string, unknown>
  effect_sha256: Sha256Hex
  program_id: string
  strategy_version: string
  sleeve_ref: string | null
  proposed_at: Instant
  send_deadline: Instant
  review: {
    quote: { bid_cents: number; ask_cents: number; market_data_type: string; as_of: Instant }
    max_loss_cents: number
    notional_cents: number
    screening_sha256: Sha256Hex
    owner_epoch: string
    profile_digest: Sha256Hex
  }
}
export interface ProposalRow {
  proposal_id: Sha256Hex
  proposal_sha256: Sha256Hex
  status: 'pending' | 'approved' | 'consumed' | 'rejected' | 'expired' | 'unknown'
  asset_kind: AssetKind
  intent_id: string
  proposed_at: Instant
  send_deadline: Instant
  proposal: EntryProposalDoc
}
/** `GET /api/proposals` (`trex-proposal-list/1`). */
export interface ProposalList {
  schema: 'trex-proposal-list/1'
  now: Instant
  proposals: ProposalRow[]
  problems: { file: string; reason: string }[]
  truncated: boolean
  execution_enabled: false
}

export interface Holding {
  account_id: string
  con_id: number
  sec_type: 'OPT' | 'STK'
  symbol: string
  quantity: number
  cost_basis_micros: number
  lots: {
    attribution: {
      intent_id: string | null
      strategy_version: string | null
      program_id: string | null
      sleeve_ref: string
    }
    quantity: number
  }[]
}
type Wave2Status<S extends string, F> =
  | ({ status: S; reason: string | null } & F)
  | ({ status: 'unavailable'; reason: string } & { [K in keyof F]: F[K] | null })
/** `GET /api/portfolio` (`trex-portfolio/1`). Wave 2 spells its reason `reason`. */
export interface PortfolioView {
  schema: 'trex-portfolio/1'
  now: Instant
  account_ids: string[]
  holdings: Wave2Status<
    'ok',
    { account_id: string; as_of: Instant; age_s: number; ledger_cash_micros: number; rows: Holding[] }
  >
  cash: Wave2Status<'fresh' | 'stale', { settled_cash_cents: number; as_of: Instant; age_s: number }>
  risk: Wave2Status<
    'fresh' | 'stale',
    {
      account_id: string
      as_of: Instant
      age_s: number
      stale_after_s: number
      open_loss_cents: number
      day_realized_loss_cents: number
      equity_gross_cents: number
    }
  >
  outside_positions: Wave2Status<
    'fresh' | 'stale',
    { rows: { con_id: number; sec_type: 'OPT' | 'STK'; symbol: string; quantity: number }[] }
  >
  reconciliation: {
    status: 'clean' | 'reconciliation_required' | 'unavailable'
    reason: string | null
    fresh: boolean
    as_of: Instant | null
    age_s: number | null
    mismatches: { con_id: number; local_qty: number; broker_qty: number; kind: string }[] | null
  }
  kill_files: Record<KillFile, boolean>
  entry_blockers: string[]
  exits_blocked: boolean
  problems: Record<string, string>[]
  execution_enabled: false
}

// ---------------------------------------------------------------- Wave 5: owner-initiated entries

// The owner starts every trade: a qualified idea (a draft listed by
// GET /api/ideas, composed with its `compose_args`) or a manual equity
// ticket (compose_entry + `manual_equity_ticket`). Composing never approves
// or sends; the approval stays a separate owner act on the proposal.
// Python: contracts/ideas.py and contracts/commands.py; fixtures:
// tests/contracts/fixtures/wave5/ (contracts.wave5.test.ts).

/** `GET /api/ideas` (`trex-idea-list/1`), relative to the app's mount. */
export const IDEAS_PATH = '/api/ideas' as const
/** A manual ticket's candidate id is this prefix plus a lower-case UUID. */
export const MANUAL_CANDIDATE_PREFIX = 'manual:' as const
/** The program of every manual ticket: the desk refuses any other
 * (`candidate_program_mismatch`). Python: `contracts.ideas.MANUAL_PROGRAM_ID`. */
export const MANUAL_PROGRAM_ID = 'owner-manual' as const
/** Review amendment: a manual ticket's exit deadline is at most this many
 * NYSE sessions after its entry session (`manual_ticket_invalid`, detail
 * `deadline_beyond_max_hold`). Python: `MANUAL_MAX_HOLD_SESSIONS`. */
export const MANUAL_MAX_HOLD_SESSIONS = 20 as const
export type ManualLimitPolicy = 'ask' | 'mid' | { limit_cents: number }

/** The owner's own whole-share, long-only stock ticket. Money in integer
 * cents. The desk prices it from a live real-time quote: `ask`; `mid`
 * rounded up to the cent; or the explicit `{limit_cents}`. */
export interface ManualEquityTicket {
  symbol: string
  quantity: number
  stop_price_cents: number
  target_price_cents?: number | null
  exit_deadline_session: IsoDate
  limit_policy: ManualLimitPolicy
}

const LOWER_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

/** `manual:<uuid>`: mint the UUID once per ticket and keep it for retries. */
export function manualCandidateId(ticketUuid: string): string {
  if (!LOWER_UUID.test(ticketUuid)) throw new RangeError('a manual ticket id is a lower-case UUID')
  return `${MANUAL_CANDIDATE_PREFIX}${ticketUuid}`
}

export interface IdeaLeg {
  right: 'C' | 'P'
  action: 'BUY' | 'SELL'
  strike_cents: number
  expiry: IsoDate
}
export interface IdeaOption {
  kind: string
  legs: IdeaLeg[]
  quantity: number
  /** The package limit per share in cents (0.90 is 90). */
  limit_cents: number
  exit_deadline: IsoDate
}
export interface IdeaEquity {
  quantity: number
  limit_cents: number
  stop_price_cents: number
  /** null: no target (the desk exits by stop or deadline). */
  target_price_cents: number | null
  exit_deadline_session: IsoDate
}
export type IdeaStatus = 'available' | 'proposed' | 'used' | 'expired'
export type IdeaProposalStatus = 'none' | 'pending' | 'approved' | 'consumed' | 'rejected' | 'expired' | 'unknown'
export type Idea = {
  candidate_id: string
  underlying: string
  intent_id: string
  program_id: string
  strategy_version: string
  summary: string
  max_loss_cents: number
  notional_cents: number
  written_at: Instant
  age_s: number
  entry_session: IsoDate
  valid_until: Instant
  /** Whole seconds left at the response's `now` (count down from receipt). */
  seconds_to_expiry: number
  status: IdeaStatus
  /** Advisory: the desk screens everything again when it composes. */
  hints: ReasonCode[]
  /** What the compose_entry command repeats, verbatim. */
  compose_args: CommandArgs['compose_entry']
} & (
  | { asset_kind: 'option'; option: IdeaOption; equity: null }
  | { asset_kind: 'equity'; option: null; equity: IdeaEquity }
) &
  ({ proposal_status: 'none'; proposal_id: null } | { proposal_status: Exclude<IdeaProposalStatus, 'none'>; proposal_id: Sha256Hex })
export interface BlockedIdea {
  candidate_id: string
  kind: string
  reason: string
  written_at: Instant
}
export interface IdeaProblem {
  file: string
  reason: ReasonCode
}
export type DraftIdeas = Block<
  'ok',
  { items: Idea[]; blocked: BlockedIdea[]; problems: IdeaProblem[]; truncated: boolean }
>
/** Review amendment (additive): `max_hold_sessions` (the hold cap) and
 * `max_exit_deadline_session` (the latest exit deadline the desk accepts for
 * a ticket entered today; null when today is not a session). A document
 * served before the amendment has neither; the parser reads them as null. */
export type ManualEquityTerms = {
  candidate_prefix: typeof MANUAL_CANDIDATE_PREFIX
  program_id: string
  strategy_version: string
  symbols: string[]
  limit_policies: ('ask' | 'mid' | 'limit_cents')[]
  max_entries_per_session: number
  hints: ReasonCode[]
  max_hold_sessions: number
} & (
  | {
      window_status: 'no_session'
      window_session: null
      window_opens_at: null
      window_closes_at: null
      max_exit_deadline_session: null
    }
  | {
      window_status: 'open' | 'closed'
      window_session: IsoDate
      window_opens_at: Instant
      window_closes_at: Instant
      max_exit_deadline_session: IsoDate
    }
)
/** `GET /api/ideas` (`trex-idea-list/1`). */
export interface IdeaList {
  schema: 'trex-idea-list/1'
  now: Instant
  drafts: DraftIdeas
  manual_equity: ManualEquityTerms
  execution_enabled: false
}

// ---------------------------------------------------------------- key lists

// Every interface's keys as data, checked at compile time against the type
// (a missing or extra key fails `tsc`) and at test time against the fixtures.

type KeysOf<T> = T extends unknown ? keyof T : never
type ExactKeys<T, K extends readonly PropertyKey[]> = [Exclude<KeysOf<T>, K[number]>] extends [never]
  ? [Exclude<K[number], KeysOf<T>>] extends [never]
    ? K
    : never
  : never

function keys<T>() {
  return <const K extends readonly KeysOf<T>[]>(list: K & ExactKeys<T, K>): K => list
}

export const KEYS = {
  TodayView: keys<TodayView>()([
    'schema',
    'now',
    'session',
    'desk_owner',
    'gateway',
    'controls',
    'approvals',
    'positions',
    'acceptance',
    'next_events',
    'blockers',
    'execution_enabled',
  ]),
  SessionState: keys<SessionState>()([
    'status',
    'unavailable_reason',
    'et_date',
    'session',
    'opens_at',
    'closes_at',
    'early_close',
    'calendar_sessions_left',
  ]),
  DeskOwnerHealth: keys<DeskOwnerHealth>()([
    'status',
    'unavailable_reason',
    'heartbeat_at',
    'heartbeat_age_s',
    'lock_held',
    'exit_owner_ready',
    'stale_after_s',
  ]),
  GatewayHealth: keys<GatewayHealth>()(['status', 'unavailable_reason', 'detail', 'checked_at', 'age_s']),
  Controls: keys<Controls>()(['pause_entries', 'halt', 'flatten', 'queued']),
  KillSwitchState: keys<KillSwitchState>()(['name', 'status', 'unavailable_reason', 'since', 'reason']),
  QueuedCommands: keys<QueuedCommands>()(['status', 'unavailable_reason', 'items']),
  QueuedCommand: keys<QueuedCommand>()(['idempotency_key', 'kind', 'issued_at', 'expires_at', 'issued_by_sub']),
  PendingApprovals: keys<PendingApprovals>()(['status', 'unavailable_reason', 'items', 'problem_count']),
  PendingApproval: keys<PendingApproval>()([
    'proposal_id',
    'proposal_sha256',
    'asset_kind',
    'intent_id',
    'program_id',
    'summary',
    'proposed_at',
    'send_deadline',
    'seconds_to_deadline',
    'max_loss_cents',
    'notional_cents',
    'approve_args',
    'precheck_blockers',
  ]),
  ApproveEntryArgs: keys<ApproveEntryArgs>()([
    'proposal_id',
    'proposal_sha256',
    'effect_sha256',
    'owner_epoch',
    'profile_digest',
  ]),
  PositionsSummary: keys<PositionsSummary>()(['holdings', 'risk']),
  HoldingsSummary: keys<HoldingsSummary>()([
    'status',
    'unavailable_reason',
    'as_of',
    'age_s',
    'option_positions',
    'equity_positions',
  ]),
  RiskSummary: keys<RiskSummary>()([
    'status',
    'unavailable_reason',
    'as_of',
    'age_s',
    'open_loss_cents',
    'day_realized_loss_cents',
    'settled_cash_cents',
    'outside_positions',
  ]),
  AcceptanceSummary: keys<AcceptanceSummary>()([
    'status',
    'unavailable_reason',
    'computed_at',
    'verdict',
    'streak_sessions',
    'option_lifecycles',
    'equity_lifecycles',
    'drills_passed',
    'streak_required',
    'lifecycles_required',
    'drills_required',
    'live_authorized',
  ]),
  NextEvents: keys<NextEvents>()(['status', 'unavailable_reason', 'items']),
  ScheduledEvent: keys<ScheduledEvent>()(['at', 'kind', 'label', 'subject']),
  HumanBlocker: keys<HumanBlocker>()(['code', 'scope', 'severity', 'message', 'next_step']),
  ActivityPage: keys<ActivityPage>()([
    'schema',
    'now',
    'limit',
    'items',
    'has_more',
    'next_cursor',
    'sources',
    'execution_enabled',
  ]),
  ActivityItem: keys<ActivityItem>()([
    'id',
    'at',
    'source',
    'kind',
    'summary',
    'actor',
    'actor_unavailable_reason',
    'command_key',
    'command_status',
    'reason',
    'refs',
  ]),
  ActivityActor: keys<ActivityActor>()(['sub', 'ingress']),
  ActivitySourceStatus: keys<ActivitySourceStatus>()(['source', 'status', 'unavailable_reason']),
  AcceptanceView: keys<AcceptanceView>()(['schema', 'now', 'status', 'unavailable_reason', 'document', 'live_authorized']),
  AcceptanceDocument: keys<AcceptanceDocument>()([
    'schema',
    'computed_at',
    'calendar_sha256',
    'owners',
    'calendar_window',
    'sessions',
    'lifecycles',
    'drills',
    'session_results',
    'lifecycle_results',
    'drill_results',
    'streak',
    'per_kind',
    'drills_passed',
    'drills_required',
    'verdict',
    'verdict_reasons',
    'live_authorized',
  ]),
  SessionRecord: keys<SessionRecord>()([
    'session',
    'heartbeat_minutes',
    'reconciliation_open',
    'reconciliation_close',
    'reconciliation_episodes',
    'unowned_orders',
    'max_exit_unguarded_s',
    'unavailable',
    'evidence',
  ]),
  LifecycleRecord: keys<LifecycleRecord>()([
    'intent_id',
    'asset_kind',
    'proposal_id',
    'approval_key',
    'approved_at',
    'filled_quantity',
    'first_fill_at',
    'exit_by',
    'exited_at',
    'broker_flat_at',
    'reconciled_at',
    'commissions_complete',
    'commissions_unavailable_reason',
    'evidence',
  ]),
  DrillRecord: keys<DrillRecord>()(['id', 'status', 'evidence', 'attested_by', 'at', 'note']),
  CalendarSession: keys<CalendarSession>()(['session', 'opens_at', 'closes_at']),
  SessionResult: keys<SessionResult>()(['session', 'recorded', 'qualifying', 'reasons']),
  LifecycleResult: keys<LifecycleResult>()(['intent_id', 'asset_kind', 'completed', 'counted', 'reasons']),
  DrillResult: keys<DrillResult>()(['id', 'passed', 'reasons']),
  StreakSummary: keys<StreakSummary>()(['current_sessions', 'current_start', 'current_end', 'best_sessions', 'required']),
  KindCounters: keys<KindCounters>()(['completed_total', 'counted', 'required']),
  PilotRegistration: keys<PilotRegistration>()([
    'schema',
    'program_id',
    'role',
    'title',
    'hypothesis',
    'universe',
    'data_sources',
    'decision_rule',
    'controls',
    'budgets',
    'existing_registration',
    'created_at',
    'status',
    'sealed_by',
    'sealed_at',
    'sha256',
  ]),
  BudgetUsage: keys<BudgetUsage>()([
    'schema',
    'program_id',
    'day',
    'requests',
    'compute_seconds',
    'storage_bytes',
    'as_of',
  ]),
  CommandBody: keys<CommandBody>()(['schema', 'idempotency_key', 'kind', 'args', 'issued_at', 'expires_at']),
  CommandResult: keys<CommandResult>()([
    'schema',
    'idempotency_key',
    'kind',
    'command_body_sha256',
    'status',
    'reason',
    'detail',
    'result',
    'at',
  ]),
  ProposalList: keys<ProposalList>()(['schema', 'now', 'proposals', 'problems', 'truncated', 'execution_enabled']),
  ProposalRow: keys<ProposalRow>()([
    'proposal_id',
    'proposal_sha256',
    'status',
    'asset_kind',
    'intent_id',
    'proposed_at',
    'send_deadline',
    'proposal',
  ]),
  EntryProposalDoc: keys<EntryProposalDoc>()([
    'schema',
    'proposal_id',
    'effect',
    'effect_sha256',
    'program_id',
    'strategy_version',
    'sleeve_ref',
    'proposed_at',
    'send_deadline',
    'review',
  ]),
  PortfolioView: keys<PortfolioView>()([
    'schema',
    'now',
    'account_ids',
    'holdings',
    'cash',
    'risk',
    'outside_positions',
    'reconciliation',
    'kill_files',
    'entry_blockers',
    'exits_blocked',
    'problems',
    'execution_enabled',
  ]),
  Holding: keys<Holding>()(['account_id', 'con_id', 'sec_type', 'symbol', 'quantity', 'cost_basis_micros', 'lots']),
  // -- Wave 5 (tests/contracts/fixtures/wave5/) --
  IdeaList: keys<IdeaList>()(['schema', 'now', 'drafts', 'manual_equity', 'execution_enabled']),
  DraftIdeas: keys<DraftIdeas>()(['status', 'unavailable_reason', 'items', 'blocked', 'problems', 'truncated']),
  Idea: keys<Idea>()([
    'candidate_id',
    'asset_kind',
    'underlying',
    'intent_id',
    'program_id',
    'strategy_version',
    'summary',
    'option',
    'equity',
    'max_loss_cents',
    'notional_cents',
    'written_at',
    'age_s',
    'entry_session',
    'valid_until',
    'seconds_to_expiry',
    'status',
    'proposal_status',
    'proposal_id',
    'hints',
    'compose_args',
  ]),
  IdeaOption: keys<IdeaOption>()(['kind', 'legs', 'quantity', 'limit_cents', 'exit_deadline']),
  IdeaLeg: keys<IdeaLeg>()(['right', 'action', 'strike_cents', 'expiry']),
  IdeaEquity: keys<IdeaEquity>()([
    'quantity',
    'limit_cents',
    'stop_price_cents',
    'target_price_cents',
    'exit_deadline_session',
  ]),
  BlockedIdea: keys<BlockedIdea>()(['candidate_id', 'kind', 'reason', 'written_at']),
  IdeaProblem: keys<IdeaProblem>()(['file', 'reason']),
  ManualEquityTerms: keys<ManualEquityTerms>()([
    'candidate_prefix',
    'program_id',
    'strategy_version',
    'symbols',
    'limit_policies',
    'max_entries_per_session',
    'window_status',
    'window_session',
    'window_opens_at',
    'window_closes_at',
    'hints',
    'max_hold_sessions',
    'max_exit_deadline_session',
  ]),
  ManualEquityTicket: keys<ManualEquityTicket>()([
    'symbol',
    'quantity',
    'stop_price_cents',
    'target_price_cents',
    'exit_deadline_session',
    'limit_policy',
  ]),
  ComposeEntryArgs: keys<CommandArgs['compose_entry']>()([
    'program_id',
    'candidate_id',
    'asset_kind',
    'sleeve_ref',
    'manual_equity_ticket',
  ]),
} as const
