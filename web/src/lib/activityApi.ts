// Activity & settings and onboarding readers (Wave 3 lane G).
//
// Read-only: GET /api/today (trex-today/1), GET /api/activity
// (trex-activity/1) and GET /api/acceptance (trex-acceptance-view/1), all
// relative to the app's mount so the same build works on loopback `/` and
// behind the portal's `/trex/`. Nothing here posts a command.
//
// The null rule (docs/product/WAVE3-CONTRACTS.md C3.2) holds on screen too: an
// unknown fact is shown as unavailable WITH its reason, never as zero, empty
// or done.

import { fetchJson } from './api'
import { ago } from './format'
import { hashFor } from './routes'
import {
  ACTIVITY_SOURCES,
  API_PATHS,
  activityQuery,
  isKnown,
  type AcceptanceView,
  type ActivityItem,
  type ActivityPage,
  type ActivitySource,
  type CommandStatus,
  type DeskOwnerHealth,
  type DrillId,
  type GatewayHealth,
  type TodayView,
} from './contracts'

/** A contract path made relative to the mount: API_PATHS.today becomes api/today. */
export function relativePath(path: string): string {
  return path.replace(/^\/+/, '')
}

export const getToday = (): Promise<TodayView> => fetchJson(relativePath(API_PATHS.today))

export const getAcceptance = (): Promise<AcceptanceView> => fetchJson(relativePath(API_PATHS.acceptance))

/** One page of the audit feed, newest first (`cursor` from the previous page). */
export const getActivity = (q: {
  limit?: number
  cursor?: string | null
  sources?: ActivitySource[]
}): Promise<ActivityPage> => fetchJson(relativePath(activityQuery(q)))

/** The contract's default page size (WAVE3-CONTRACTS.md default 18: 50, at most 200). */
export const ACTIVITY_PAGE_SIZE = 50

// ---------------------------------------------------------------- labels

export const SOURCE_LABELS: Record<ActivitySource, string> = {
  command: 'Commands',
  desk_event: 'Desk events',
  audit: 'Audit',
  drill: 'Drills',
}

export const INGRESS_LABELS: Record<'family' | 'loopback' | 'system', string> = {
  family: 'Family Apps (phone or desktop)',
  loopback: 'this computer (loopback)',
  system: 'the TREX desk (automatic)',
}

export const COMMAND_STATUS_LABELS: Record<CommandStatus, string> = {
  accepted: 'Queued, not applied yet',
  applied: 'Applied by the desk',
  refused: 'Refused',
  failed: 'Failed',
  duplicate: 'Duplicate (first outcome kept)',
}

export const DRILL_LABELS: Record<DrillId, string> = {
  relogin: 'Log back in after a gateway logout',
  restart_mid_position: 'Restart the desk with a position open',
  gateway_outage: 'Ride out a gateway outage',
  duplicate_command: 'Send the same command twice',
  backup_restore: 'Back up and restore the desk',
  notification: 'Receive a problem alert',
  unassisted_use: 'Use TREX without help',
}

/** Plain words for the machine reasons a page can show. Unlisted codes fall
 * back to the code itself, spaced; the code is always shown beside the words
 * (`reasonText`). */
export const REASON_WORDS: Record<string, string> = {
  // Wave 3 unavailable reasons and Today blockers (Wave3Reason)
  calendar_unreadable: 'The market calendar could not be read',
  calendar_exhausted: 'The market calendar has no more sessions',
  heartbeat_absent: 'The desk owner has written no heartbeat',
  runtime_lock_unknown: 'Whether the desk owner holds its lock is unknown',
  gateway_watch_not_reporting: 'The IB Gateway watch is not reporting',
  gateway_watch_stale: 'The IB Gateway watch has not reported recently',
  kill_file_unreadable: 'The stop files could not be read',
  proposals_dir_unreadable: 'The proposals folder could not be read',
  acceptance_missing: 'No acceptance record has been computed yet',
  acceptance_invalid: 'The acceptance record fails its own checks',
  acceptance_calendar_mismatch: 'The acceptance record was judged against another calendar',
  acceptance_window_mismatch: 'The acceptance record leaves sessions out',
  schedule_unavailable: 'The schedule could not be worked out',
  activity_source_unreadable: 'This activity source could not be read',
  actor_not_recorded: 'This older record does not say who acted',
  desk_owner_stopped: 'The desk owner is stopped',
  desk_owner_stale: 'The desk owner heartbeat is stale',
  gateway_needs_login: 'IB Gateway needs a login',
  gateway_needs_2fa: 'IB Gateway is waiting for 2FA',
  gateway_starting: 'IB Gateway is starting',
  gateway_api_down: 'The IB Gateway API is not answering',
  gateway_down: 'IB Gateway is down',
  exit_unguarded: 'An open position has no exit watch',
  calendar_horizon_near: 'The market calendar is running out',
  family_commands_disabled: 'Commands from Family Apps are turned off',
  // acceptance: sessions
  exit_guard_unavailable: 'How long exits went unwatched was not recorded',
  exit_unguarded_over_5min: 'An open position went unwatched for over 5 minutes',
  heartbeat_below_99pct: 'The desk heartbeat was fresh for less than 99% of the session',
  heartbeat_minutes_exceed_session: 'More heartbeat minutes were recorded than the session has',
  heartbeat_unavailable: 'Heartbeat coverage was not recorded',
  reconciliation_not_clean_at_close: 'The books did not match the broker at the close',
  reconciliation_open_unresolved: 'A mismatch at the open was not taken up in time',
  reconciliation_unavailable: 'Reconciliation with the broker is unavailable',
  reconciliation_unresolved_in_session: 'A mismatch was still open at the close',
  session_not_recorded: 'This session was not recorded',
  unowned_orders_present: 'The broker had orders TREX does not own',
  unowned_orders_unavailable: 'Orders TREX does not own were not counted',
  // acceptance: paper trades (lifecycles)
  broker_not_flat: 'The broker does not show the position closed',
  commissions_missing: 'Commissions are missing for some fills',
  commissions_unknown: 'Commissions are not known yet',
  exit_not_by_policy: 'The exit was manual, not by policy or close-all',
  not_exited: 'The position has not exited',
  not_filled: 'The entry never filled',
  not_reconciled: 'The books were not reconciled clean after the exit',
  // acceptance: drills and verdict
  attester_not_owner: 'Attested by someone who is not an owner',
  drill_failed: 'The drill failed',
  drill_not_run: 'The drill has not been run',
  drill_not_passed: 'A drill has not passed',
  equity_lifecycles_short: 'Too few completed equity paper trades',
  option_lifecycles_short: 'Too few completed option paper trades',
  streak_short: 'The qualifying-session streak is too short',
  // Wave 2 view reasons and blockers the pages show most
  holdings_missing: 'The books have not written holdings yet',
  holdings_unreadable: 'The holdings file could not be read',
  holdings_invalid: 'The holdings file is not valid',
  holdings_account_mismatch: 'The holdings name more than one account',
  risk_snapshot_missing: 'No risk snapshot has been written yet',
  risk_snapshot_unreadable: 'The risk snapshot could not be read',
  risk_snapshot_invalid: 'The risk snapshot is not valid',
  risk_snapshot_unavailable: 'The broker could not price the account',
  risk_snapshot_stale: 'The risk snapshot is too old',
  reconciliation_missing: 'No reconciliation has been written yet',
  reconciliation_unreadable: 'The reconciliation could not be read',
  reconciliation_invalid: 'The reconciliation is not valid',
  reconciliation_stale: 'The last reconciliation is too old',
  reconciliation_required: "TREX's books and the broker disagree",
  account_mismatch: 'The books name more than one account',
  non_paper_account: 'An account is not a paper account',
  entries_paused: 'New entries are paused',
  halt: 'Emergency stop is standing',
  flatten: 'Close all desk positions is standing',
  exit_owner_not_ready: 'Nobody is watching exits',
  equity_quote_not_realtime: 'Equity quotes are delayed',
  entry_window_closed: 'The entry window is closed',
  outside_regular_session: 'Outside regular market hours',
  session_entry_limit_reached: 'The per-session entry limit is reached',
  insufficient_cash: 'Not enough settled cash',
  profile_absent: 'No capital profile is installed',
  profile_invalid: 'The capital profile is not valid',
  profile_limits_missing: 'The capital profile sets no loss limits',
  command_store_unavailable: 'The command queue could not be read',
  command_expired: 'The command expired before the desk applied it',
  command_unauthorized: 'The command was not authorized',
  command_body_mismatch: 'A different command reused the same key',
  duplicate_command: 'The same command was sent twice',
  approval_expired: 'The approval ran out of time',
  approval_epoch_mismatch: 'The desk restarted after the proposal was made',
  approval_profile_mismatch: 'The capital profile changed after the proposal',
  approval_effect_mismatch: 'The approval does not match the proposal',
  approval_proposal_mismatch: 'The approval does not match the proposal',
  proposal_unknown: 'The desk does not know this proposal',
  proposal_rejected: 'The proposal was rejected',
  broker_rejected: 'The broker rejected the order',
  gateway_disconnected: 'IB Gateway is disconnected',
}

/** Plain words for a reason code (`drill_not_passed:<id>` names the drill). */
export function reasonWords(code: string): string {
  const [head, drill] = code.split(':', 2)
  if (head === 'drill_not_passed' && drill !== undefined && drill in DRILL_LABELS) {
    return `Drill not passed: ${DRILL_LABELS[drill as DrillId]}`
  }
  return REASON_WORDS[code] ?? code.replace(/_/g, ' ')
}

/** The words and, in brackets, the machine code. */
export function reasonText(code: string): string {
  return `${reasonWords(code)} (${code})`
}

// ---------------------------------------------------------------- the feed

/** Who acted and through which door, or why that is not known. */
export function actorText(item: ActivityItem): string {
  if (item.actor !== null) return `${item.actor.sub} via ${INGRESS_LABELS[item.actor.ingress]}`
  return `Actor not recorded: ${reasonText(item.actor_unavailable_reason)}`
}

/** Append an older page, keeping the first copy of any repeated item. */
export function appendPage(items: ActivityItem[], page: ActivityPage): ActivityItem[] {
  const seen = new Set(items.map((item) => item.id))
  return [...items, ...page.items.filter((item) => !seen.has(item.id))]
}

/** Toggle one source in a filter where `[]` means every source; the result
 * is in contract order and selecting every source collapses back to `[]`. */
export function toggleSource(selected: ActivitySource[], source: ActivitySource): ActivitySource[] {
  const next = new Set(selected)
  if (next.has(source)) next.delete(source)
  else next.add(source)
  const ordered = ACTIVITY_SOURCES.filter((s) => next.has(s))
  return ordered.length === ACTIVITY_SOURCES.length ? [] : ordered
}

export const isProposalId = (value: string): boolean => /^[0-9a-f]{64}$/.test(value)

// ---------------------------------------------------------------- onboarding

export type StepState = 'done' | 'todo' | 'unknown'
export type OnboardingStepId = 'gateway' | 'heartbeat' | 'profile' | 'family_key' | 'first_approval'

export interface OnboardingStep {
  id: OnboardingStepId
  title: string
  state: StepState
  /** What Today shows for this step. */
  detail: string
  /** What to do next ('' when nothing). */
  next: string
  /** Where to do it, or null. */
  href: string | null
}

export const ONBOARDING_TITLES: Record<OnboardingStepId, string> = {
  gateway: 'IB Gateway logged in',
  heartbeat: 'Desk owner heartbeat',
  profile: 'Capital profile present',
  family_key: 'Family command key installed',
  first_approval: 'First paper approval',
}

/** Today blocker codes that mean the capital profile is not usable. */
export const PROFILE_BLOCKERS: readonly string[] = ['profile_absent', 'profile_invalid', 'profile_limits_missing']
/** The Today blocker that says family commands are disabled. Its PRESENCE is
 * evidence (To do); its absence is not: trex-today/1 carries no positive fact
 * about Family command ingress, so without the blocker the step is Unknown. */
export const FAMILY_DISABLED = 'family_commands_disabled'

/** Why the family key step is Unknown while Today has no positive fact. */
export const FAMILY_UNKNOWN_DETAIL =
  'Today has no positive fact about Family command ingress, so it cannot show whether the TREX command key is ' +
  'installed. The copy of the key on the Family Apps portal cannot be seen from TREX at all.'

type Outcome = Omit<OnboardingStep, 'id' | 'title'>
const done = (detail: string): Outcome => ({ state: 'done', detail, next: '', href: null })
const todo = (detail: string, next: string, href: string | null = null): Outcome => ({ state: 'todo', detail, next, href })
const unknown = (detail: string, next: string, href: string | null = null): Outcome => ({ state: 'unknown', detail, next, href })

function gatewayStep(gateway: GatewayHealth): Outcome {
  if (gateway.status === 'unavailable') {
    return unknown(
      `Gateway health is unknown: ${reasonText(gateway.unavailable_reason)}.`,
      'Check that the gateway watch runs on the host; Today shows the gateway again once it reports.',
    )
  }
  switch (gateway.status) {
    case 'ok':
      return done(`Logged in and answering (${gateway.detail}), checked ${ago(gateway.age_s)} ago.`)
    case 'needs_login':
      return todo(
        `IB Gateway needs a login (${gateway.detail}).`,
        'Open the gateway screen from a tailnet device, choose Paper Trading and log in.',
      )
    case 'needs_2fa':
      return todo(`IB Gateway is waiting for 2FA (${gateway.detail}).`, 'Approve the login prompt in IBKR Mobile.')
    case 'starting':
    case 'checking':
      return todo(`IB Gateway is ${gateway.status} (${gateway.detail}).`, 'Wait a minute, then refresh this page.')
    default:
      return todo(
        `IB Gateway is not answering (${gateway.detail}).`,
        'The gateway watch restarts it on its own, at most once every 2 hours. If it stays down, log in by hand.',
      )
  }
}

function heartbeatStep(owner: DeskOwnerHealth): Outcome {
  if (owner.status === 'unavailable') {
    return unknown(`The heartbeat is unknown: ${reasonText(owner.unavailable_reason)}.`, 'Check the desk service on the host.')
  }
  const age = `${ago(owner.heartbeat_age_s)} old; it counts as stale after ${owner.stale_after_s}s`
  switch (owner.status) {
    case 'healthy':
      return done(`Fresh: ${age}. Open positions have a watching exit owner.`)
    case 'stale':
      return todo(`The heartbeat is ${age}. Exits may be unwatched.`, 'Check the desk service on the host.')
    default:
      return todo(
        `The desk owner is stopped (heartbeat ${age}); nobody is watching exits.`,
        'Start the desk service on the host.',
      )
  }
}

function countedTrades(today: TodayView): number | null {
  return isKnown(today.acceptance) ? today.acceptance.option_lifecycles + today.acceptance.equity_lifecycles : null
}

function heldPositions(today: TodayView): number | null {
  const holdings = today.positions.holdings
  return isKnown(holdings) ? holdings.option_positions + holdings.equity_positions : null
}

function profileStep(today: TodayView): Outcome {
  const blocker = today.blockers.find((b) => PROFILE_BLOCKERS.includes(b.code))
  if (blocker) {
    return todo(
      `${blocker.message} (${blocker.code})`,
      blocker.next_step || 'Install a valid capital profile (profile.json) in the desk run directory.',
    )
  }
  const waiting = isKnown(today.approvals) ? today.approvals.items[0] : undefined
  if (waiting) return done(`A waiting proposal is bound to profile ${waiting.approve_args.profile_digest.slice(0, 12)}.`)
  if ((countedTrades(today) ?? 0) > 0 || (heldPositions(today) ?? 0) > 0) {
    return done('The desk has entered paper trades, which it does only under a valid profile.')
  }
  if (!isKnown(today.approvals)) {
    return unknown(
      `Today names no profile problem, and the proposals that would confirm the profile are unknown: ${reasonText(today.approvals.unavailable_reason)}.`,
      'Nothing to do unless Today reports a profile problem.',
    )
  }
  return unknown(
    'Today names no profile problem, but it can confirm the profile only through a proposal, and none is waiting.',
    'Nothing to do unless Today reports a profile problem.',
  )
}

function familyStep(today: TodayView): Outcome {
  const blocker = today.blockers.find((b) => b.code === FAMILY_DISABLED)
  if (blocker) {
    return todo(
      `${blocker.message} (${blocker.code})`,
      blocker.next_step || 'Install the same command key file on the Family Apps portal and on TREX.',
    )
  }
  return unknown(
    FAMILY_UNKNOWN_DETAIL,
    `Your first approval from the phone settles it: once it reaches the desk, Activity lists it "via ${INGRESS_LABELS.family}".`,
    hashFor('activity'),
  )
}

function firstApprovalStep(today: TodayView): Outcome {
  const counted = countedTrades(today)
  const held = heldPositions(today)
  if (counted !== null && counted > 0) {
    return done(`Paper acceptance counts ${counted} completed paper trade${counted === 1 ? '' : 's'}.`)
  }
  if (held !== null && held > 0) {
    return done(`TREX holds ${held} desk position${held === 1 ? '' : 's'}; every desk entry needs your approval.`)
  }
  const waiting = isKnown(today.approvals) ? today.approvals.items[0] : undefined
  const next = waiting
    ? 'A proposal is waiting: review it and approve or reject it before its countdown ends.'
    : 'When the desk proposes an entry, Today shows it with a countdown: approve it there or in Trade review.'
  const href = waiting ? hashFor('trade-review-proposal', waiting.proposal_id) : hashFor('today')
  if (counted === null || held === null) {
    const reasons: string[] = []
    if (!isKnown(today.acceptance)) reasons.push(reasonText(today.acceptance.unavailable_reason))
    if (!isKnown(today.positions.holdings)) reasons.push(reasonText(today.positions.holdings.unavailable_reason))
    return unknown(`Not known: ${reasons.join('; ')}.`, next, href)
  }
  return todo('No approved paper trade yet.', next, href)
}

/** The first-run checklist, in order, from one `trex-today/1` read. Without
 * a read (`today` null) every step is unknown and says why. */
export function onboardingSteps(today: TodayView | null, error: string | null = null): OnboardingStep[] {
  const ids: OnboardingStepId[] = ['gateway', 'heartbeat', 'profile', 'family_key', 'first_approval']
  if (today === null) {
    const why = `Today could not be read${error ? `: ${error}` : ''}.`
    return ids.map((id) => ({ id, title: ONBOARDING_TITLES[id], ...unknown(why, 'Refresh once the connection is back.') }))
  }
  const outcomes: Record<OnboardingStepId, Outcome> = {
    gateway: gatewayStep(today.gateway),
    heartbeat: heartbeatStep(today.desk_owner),
    profile: profileStep(today),
    family_key: familyStep(today),
    first_approval: firstApprovalStep(today),
  }
  return ids.map((id) => ({ id, title: ONBOARDING_TITLES[id], ...outcomes[id] }))
}
