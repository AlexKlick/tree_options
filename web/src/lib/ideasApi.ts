// Owner-initiated entries (Wave 5 lane web): the ideas the desk qualified and
// the owner's manual SPY ticket, both turned into ONE `compose_entry` command.
//
// Rules that hold here:
//
// - Composing is not approving. A `compose_entry` asks the desk owner to
//   screen a candidate and write a `trex-entry-proposal/1`; nothing is sent.
//   The owner then approves that proposal as a separate act on its Trade
//   review page (the existing Approve step with its countdown). Nothing in
//   this module builds any other command kind.
// - One press is one request: `prepareCompose` freezes the key, the issue
//   time and the bytes (`commandsApi.prepareCommand`), and a retry re-sends
//   them, so a retry can only be the store's `duplicate`. The manual ticket's
//   candidate id is `manual:<the request key>`, so one press always names one
//   candidate.
// - Instants come from the desk's clock (`serverNowMs` on the ideas
//   response), never this device's clock; countdowns start from the
//   server's own `seconds_to_expiry`.
// - URLs are relative (no leading slash): `/` on loopback, `/trex/` through
//   the Family portal.
// - Hints are advisory: they are shown in words, never used to refuse here;
//   the desk screens everything again with its live quote.
//
// Wire shapes: `GET /api/ideas` (`trex-idea-list/1`) and `compose_entry`'s
// `manual_equity_ticket` are the KEYS-pinned types of contracts.ts (`IdeaList`,
// `Idea`, `ManualEquityTerms`, `ManualEquityTicket`, ...), which
// contracts.wave5.test.ts checks against the shared Wave 5 fixtures. This
// module keeps only its READ model (what the page shows), each field typed
// from those contract types, and `parseIdeas` reads the document defensively:
// a field it does not find shows as "not stated", never as a guess
// (ideasApi.test.ts reads the shared fixtures through it).

import { fetchJson } from './api'
import {
  COMMAND_MAX_TTL_S,
  IDEAS_PATH,
  MANUAL_CANDIDATE_PREFIX,
  MANUAL_PROGRAM_ID,
  type BlockedIdea,
  type CommandArgs,
  type Idea,
  type IdeaList,
  type IdeaProblem,
  type IdempotencyKey,
  type Instant,
  type IsoDate,
  type ManualEquityTerms,
  type ManualEquityTicket,
  type ManualLimitPolicy,
} from './contracts'
import { newIdempotencyKey, prepareCommand, relativeUrl, type PreparedCommand } from './commandsApi'

// ---------------------------------------------------------------- wire shapes (contracts.ts)

export { IDEAS_PATH, MANUAL_CANDIDATE_PREFIX, MANUAL_PROGRAM_ID }
export type { ManualEquityTicket, ManualLimitPolicy }
export const IDEAS_URL = relativeUrl(IDEAS_PATH)
export const IDEA_LIST_SCHEMA: IdeaList['schema'] = 'trex-idea-list/1'
/** contracts.defaults `EQUITY_SYMBOL_ALLOWLIST` (SPY only); the terms may narrow it. */
export const DEFAULT_EQUITY_SYMBOLS: readonly string[] = ['SPY']

/** `compose_entry` args: Wave 2's plus the optional manual ticket. */
export type ComposeEntryArgs = CommandArgs['compose_entry']

// ---------------------------------------------------------------- what this page reads

/** The form's limit choice (`limit` = an explicit `{limit_cents}`). */
export type LimitPolicy = 'ask' | 'mid' | 'limit'
export const LIMIT_POLICIES: readonly LimitPolicy[] = ['ask', 'mid', 'limit']

export interface QuoteFacts {
  bid_cents: number
  ask_cents: number
  market_data_type: string
  as_of: Instant
}

/** One drafted idea (`Idea`) as this page reads it. `status`: `available`
 * (preparable), `proposed` (a live proposal names it: `proposal_id`), `used`,
 * `expired`; an unknown value shows as its own words. Null: not stated. */
export interface IdeaRow {
  candidate_id: Idea['candidate_id']
  program_id: Idea['program_id']
  asset_kind: Idea['asset_kind']
  summary: Idea['summary'] | null
  status: Idea['status'] | (string & {})
  proposal_status: Idea['proposal_status'] | (string & {}) | null
  proposal_id: Idea['proposal_id']
  max_loss_cents: Idea['max_loss_cents'] | null
  notional_cents: Idea['notional_cents'] | null
  valid_until: Idea['valid_until'] | null
  /** The server's whole seconds to `valid_until` at `now` (null: not stated). */
  seconds_to_expiry: Idea['seconds_to_expiry'] | null
  /** Advisory blockers the web can see (the desk is authoritative). */
  hints: string[]
  /** The exact args the compose command repeats (null: not stated). */
  compose_args: ComposeEntryArgs | null
}

/** A deal the miner could not draft (`BlockedIdea`, a `<candidate>.BLOCKED.json`). */
export type BlockedIdeaRow = Pick<BlockedIdea, 'candidate_id' | 'kind' | 'reason'>

/** What the manual ticket form needs (`ManualEquityTerms`), as this page
 * reads it. Null: not stated (`max_hold_sessions` and
 * `max_exit_deadline_session` are absent from a document served before the
 * review amendment). */
export interface ManualTicketTerms {
  program_id: ManualEquityTerms['program_id'] | null
  symbols: ManualEquityTerms['symbols']
  limit_policies: LimitPolicy[]
  max_entries_per_session: ManualEquityTerms['max_entries_per_session'] | null
  window_status: ManualEquityTerms['window_status'] | null
  window_session: IsoDate | null
  window_opens_at: Instant | null
  window_closes_at: Instant | null
  hints: string[]
  max_hold_sessions: ManualEquityTerms['max_hold_sessions'] | null
  max_exit_deadline_session: IsoDate | null
  /** Not in `trex-idea-list/1` (null) unless a later amendment adds them. */
  sessions: IsoDate[] | null
  quote: QuoteFacts | null
}

/** `GET /api/ideas` (`IdeaList`), as this page reads it. */
export interface IdeasView {
  schema: IdeaList['schema']
  now: Instant
  /** The drafts block: `unavailable` with `unavailable_reason` when the desk
   * could not list the drafts (then no idea is known). */
  status: 'ok' | 'unavailable'
  unavailable_reason: string | null
  ideas: IdeaRow[]
  blocked: BlockedIdeaRow[]
  problems: Pick<IdeaProblem, 'file' | 'reason'>[]
  /** Items this page could not read (counted, never silently dropped). */
  unreadable: number
  truncated: boolean
  manual_equity: ManualTicketTerms | null
}

// ---------------------------------------------------------------- parsing

type Rec = Record<string, unknown>
const isRec = (v: unknown): v is Rec => typeof v === 'object' && v !== null && !Array.isArray(v)
const isStr = (v: unknown): v is string => typeof v === 'string'
const isInt = (v: unknown): v is number => Number.isInteger(v)
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/
const SHA256 = /^[0-9a-f]{64}$/
const strOrNull = (v: unknown): string | null => (isStr(v) && v !== '' ? v : null)
const intOrNull = (v: unknown): number | null => (isInt(v) ? v : null)
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter(isStr) : [])

function instantOrNull(v: unknown): Instant | null {
  return isStr(v) && !Number.isNaN(Date.parse(v)) ? v : null
}

function dateOrNull(v: unknown): IsoDate | null {
  return isStr(v) && ISO_DATE.test(v) ? v : null
}

function sessionsOf(v: unknown): IsoDate[] | null {
  if (!Array.isArray(v)) return null
  return [...new Set(v.filter((d): d is string => isStr(d) && ISO_DATE.test(d)))].sort()
}

function quoteOf(v: unknown): QuoteFacts | null {
  if (!isRec(v)) return null
  const { bid_cents, ask_cents, market_data_type } = v
  if (!isInt(bid_cents) || !isInt(ask_cents) || bid_cents < 0 || ask_cents < bid_cents) return null
  const at = instantOrNull(v.as_of)
  if (at === null) return null // a quote without its time is not one to lean on
  return { bid_cents, ask_cents, market_data_type: isStr(market_data_type) ? market_data_type : 'unknown', as_of: at }
}

function composeArgsOf(v: unknown): ComposeEntryArgs | null {
  if (!isRec(v)) return null
  const { program_id, candidate_id, asset_kind } = v
  if (!isStr(program_id) || !isStr(candidate_id) || (asset_kind !== 'option' && asset_kind !== 'equity')) return null
  // a drafted idea composes without a manual ticket; sleeve_ref rides along when set
  const args: ComposeEntryArgs = { program_id, candidate_id, asset_kind }
  if (isStr(v.sleeve_ref)) args.sleeve_ref = v.sleeve_ref
  return args
}

function ideaOf(v: unknown): IdeaRow | null {
  if (!isRec(v)) return null
  const { candidate_id, program_id, asset_kind } = v
  if (!isStr(candidate_id) || !candidate_id || !isStr(program_id) || !program_id) return null
  if (asset_kind !== 'option' && asset_kind !== 'equity') return null
  const args = composeArgsOf(v.compose_args)
  const proposalId = isStr(v.proposal_id) && SHA256.test(v.proposal_id) ? v.proposal_id : null
  return {
    candidate_id,
    program_id,
    asset_kind,
    summary: strOrNull(v.summary),
    status: isStr(v.status) && v.status ? v.status : 'unknown',
    proposal_status: strOrNull(v.proposal_status),
    proposal_id: proposalId,
    max_loss_cents: intOrNull(v.max_loss_cents),
    notional_cents: intOrNull(v.notional_cents),
    valid_until: instantOrNull(v.valid_until),
    seconds_to_expiry: isInt(v.seconds_to_expiry) && v.seconds_to_expiry >= 0 ? v.seconds_to_expiry : null,
    hints: strings(v.hints),
    // the args must name this idea, else they are not used
    compose_args:
      args !== null && args.candidate_id === candidate_id && args.asset_kind === asset_kind && args.program_id === program_id
        ? args
        : null,
  }
}

function blockedOf(v: unknown): BlockedIdeaRow | null {
  if (!isRec(v) || !isStr(v.candidate_id) || !isStr(v.reason)) return null
  return { candidate_id: v.candidate_id, kind: isStr(v.kind) ? v.kind : 'unknown', reason: v.reason }
}

function termsOf(v: unknown): ManualTicketTerms | null {
  if (!isRec(v)) return null
  const policies = strings(v.limit_policies)
    .map((p) => (p === 'limit_cents' ? 'limit' : p))
    .filter((p): p is LimitPolicy => (LIMIT_POLICIES as readonly string[]).includes(p))
  const status = v.window_status
  const hold = intOrNull(v.max_hold_sessions)
  return {
    program_id: strOrNull(v.program_id),
    symbols: Array.isArray(v.symbols) ? strings(v.symbols) : [...DEFAULT_EQUITY_SYMBOLS],
    limit_policies: policies.length > 0 ? policies : [...LIMIT_POLICIES],
    max_entries_per_session: intOrNull(v.max_entries_per_session),
    window_status: status === 'open' || status === 'closed' || status === 'no_session' ? status : null,
    window_session: dateOrNull(v.window_session),
    window_opens_at: instantOrNull(v.window_opens_at),
    window_closes_at: instantOrNull(v.window_closes_at),
    hints: strings(v.hints),
    max_hold_sessions: hold !== null && hold >= 1 ? hold : null,
    max_exit_deadline_session: dateOrNull(v.max_exit_deadline_session),
    sessions: sessionsOf(v.sessions),
    quote: quoteOf(v.quote),
  }
}

function listOf<T>(v: unknown, read: (item: unknown) => T | null): { items: T[]; unreadable: number } {
  const rows = Array.isArray(v) ? v : []
  const items = rows.map(read).filter((r): r is T => r !== null)
  return { items, unreadable: rows.length - items.length }
}

/** Read a `GET api/ideas` document; throws when it is not an idea list at
 * all (the page then says the list could not be loaded). */
export function parseIdeas(doc: unknown): IdeasView {
  if (!isRec(doc) || doc.schema !== IDEA_LIST_SCHEMA) throw new Error(`not a ${IDEA_LIST_SCHEMA} document`)
  const now = instantOrNull(doc.now)
  if (now === null) throw new Error('the idea list has no server time')
  const drafts = isRec(doc.drafts) ? doc.drafts : null
  const unavailable = drafts === null || drafts.status !== 'ok'
  const ideas = listOf(drafts?.items, ideaOf)
  const blocked = listOf(drafts?.blocked, blockedOf)
  const problems = Array.isArray(drafts?.problems)
    ? drafts.problems.filter((p): p is { file: string; reason: string } => isRec(p) && isStr(p.file) && isStr(p.reason))
    : []
  return {
    schema: IDEA_LIST_SCHEMA,
    now,
    status: unavailable ? 'unavailable' : 'ok',
    // no drafts block at all: this page cannot read the answer (a client code)
    unavailable_reason: unavailable ? (strOrNull(drafts?.unavailable_reason) ?? 'unexpected_response') : null,
    ideas: ideas.items,
    blocked: blocked.items,
    problems,
    unreadable: ideas.unreadable + blocked.unreadable,
    truncated: drafts?.truncated === true,
    manual_equity: termsOf(doc.manual_equity),
  }
}

export const getIdeas = async (): Promise<IdeasView> => parseIdeas(await fetchJson<unknown>(IDEAS_URL))

/** The sessions an exit deadline may name, when the server lists them;
 * otherwise null (a free date the desk validates). */
export function deadlineSessions(view: IdeasView): IsoDate[] | null {
  return view.manual_equity?.sessions ?? null
}

/** Where the equity entry window stands at `deskNowMs` (the desk's clock):
 * computed from the server's window so it moves while the page is open. */
export function entryWindowState(
  terms: ManualTicketTerms | null,
  deskNowMs: number,
): 'open' | 'not_yet' | 'closed' | 'no_session' | 'unknown' {
  if (terms === null) return 'unknown'
  if (terms.window_status === 'no_session') return 'no_session'
  if (terms.window_opens_at !== null && terms.window_closes_at !== null) {
    if (deskNowMs < Date.parse(terms.window_opens_at)) return 'not_yet'
    return deskNowMs < Date.parse(terms.window_closes_at) ? 'open' : 'closed'
  }
  return terms.window_status ?? 'unknown'
}

// ---------------------------------------------------------------- commands

/** An idea is offered for preparation only while the server lists it as
 * available and time is left on it (counted from the response). */
export function ideaPreparable(idea: IdeaRow, secondsLeftOnIdea: number | null): boolean {
  return idea.status === 'available' && (secondsLeftOnIdea === null || secondsLeftOnIdea > 0)
}

/** The `compose_entry` args for a listed idea: the server's `compose_args`
 * verbatim, else exactly its own ids. */
export function composeArgsForIdea(
  idea: Pick<IdeaRow, 'program_id' | 'candidate_id' | 'asset_kind'> & { compose_args?: ComposeEntryArgs | null },
): ComposeEntryArgs {
  if (idea.compose_args) return { ...idea.compose_args }
  return { program_id: idea.program_id, candidate_id: idea.candidate_id, asset_kind: idea.asset_kind }
}

/** The `compose_entry` args for a manual ticket; the candidate is
 * `manual:<request key>`, so a retry of this press names the same candidate. */
export function composeArgsForTicket(
  ticket: ManualEquityTicket,
  key: IdempotencyKey,
  programId: string = MANUAL_PROGRAM_ID,
): ComposeEntryArgs {
  return {
    program_id: programId,
    candidate_id: `${MANUAL_CANDIDATE_PREFIX}${key}`,
    asset_kind: 'equity',
    manual_equity_ticket: ticket,
  }
}

/** A compose command lives at most 300 s and never past the idea's horizon. */
export function composeTtlS(secondsLeftOnIdea: number | null): number {
  if (secondsLeftOnIdea === null) return COMMAND_MAX_TTL_S
  if (!Number.isInteger(secondsLeftOnIdea) || secondsLeftOnIdea < 1) throw new RangeError('the idea has expired')
  return Math.min(COMMAND_MAX_TTL_S, secondsLeftOnIdea)
}

/** One frozen `compose_entry` (the only kind this module ever prepares). */
export function prepareCompose(
  args: ComposeEntryArgs,
  issuedAtMs: number,
  ttlS: number = COMMAND_MAX_TTL_S,
  key: IdempotencyKey = newIdempotencyKey(),
): PreparedCommand<'compose_entry'> {
  return prepareCommand('compose_entry', args, issuedAtMs, ttlS, key)
}

/** The new proposal an applied `compose_entry` names (64 lower-case hex), if any. */
export function composedProposalId(result: { kind: string; status: string; result: Record<string, unknown> | null }): string | null {
  if (result.kind !== 'compose_entry' || result.status !== 'applied') return null
  const id = result.result?.proposal_id
  return isStr(id) && SHA256.test(id) ? id : null
}

// ---------------------------------------------------------------- the manual ticket

export interface TicketForm {
  quantity: string
  limitPolicy: LimitPolicy
  /** Dollars, used only with the `limit` policy. */
  limitPrice: string
  stopPrice: string
  /** Dollars; blank means no target. */
  targetPrice: string
  /** An ISO date. */
  deadline: string
}

export const EMPTY_TICKET: TicketForm = {
  quantity: '',
  limitPolicy: 'ask',
  limitPrice: '',
  stopPrice: '',
  targetPrice: '',
  deadline: '',
}

export type TicketField = 'quantity' | 'limitPrice' | 'stopPrice' | 'targetPrice' | 'deadline'
export type TicketErrors = Partial<Record<TicketField, string>>

export interface TicketContext {
  /** The desk's clock now. */
  now: Instant
  symbol: string
  /** Upcoming sessions the deadline must be one of, or null (free date). */
  sessions: IsoDate[] | null
  /** A real-time quote, when the server gives one (drives the hint). */
  quote: QuoteFacts | null
  /** The latest exit deadline the desk accepts today (the terms'
   * `max_exit_deadline_session`), or null/absent (the desk alone judges). */
  maxDeadline?: IsoDate | null
  /** The hold cap behind it (`max_hold_sessions`), for the words. */
  maxHoldSessions?: number | null
}

export type TicketCheck =
  | { ok: true; ticket: ManualEquityTicket; entryHintCents: number | null }
  | { ok: false; errors: TicketErrors; entryHintCents: number | null }

const DOLLARS = /^(\d{1,7})(?:\.(\d{1,2}))?$/

/** Dollars typed by the owner as integer cents (`671.5` -> 67150), or null.
 * At most two decimals: a sub-cent price is refused, never rounded. */
export function parseDollars(text: string): number | null {
  const cleaned = text.trim().replace(/^\$/, '').replace(/,/g, '')
  const m = DOLLARS.exec(cleaned)
  if (!m) return null
  return Number(m[1]) * 100 + Number((m[2] ?? '').padEnd(2, '0'))
}

/** The entry price a policy implies, in cents, when it can be known here:
 * the owner's limit, or the quote's ask or midpoint (rounded up, as the desk). */
export function entryHintCents(policy: LimitPolicy, limitCents: number | null, quote: QuoteFacts | null): number | null {
  if (policy === 'limit') return limitCents
  if (quote === null) return null
  return policy === 'ask' ? quote.ask_cents : Math.ceil((quote.bid_cents + quote.ask_cents) / 2)
}

const etDateFmt = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

/** The America/New_York calendar date of an instant (`YYYY-MM-DD`). */
export function etDate(at: Instant): IsoDate {
  return etDateFmt.format(new Date(at))
}

/** Check the owner's ticket; the desk checks every rule again at compose. */
export function validateTicket(form: TicketForm, ctx: TicketContext): TicketCheck {
  const errors: TicketErrors = {}
  const qtyText = form.quantity.trim()
  let quantity = 0
  if (qtyText === '') errors.quantity = 'Enter how many shares to buy.'
  else if (/^\d*[.,]\d*$/.test(qtyText)) errors.quantity = 'Whole shares only: no fractions.'
  else if (!/^\d+$/.test(qtyText)) errors.quantity = 'Enter a whole number of shares.'
  else {
    quantity = Number(qtyText)
    if (!Number.isSafeInteger(quantity) || quantity < 1) errors.quantity = 'Buy at least 1 share.'
  }

  let limitCents: number | null = null
  if (form.limitPolicy === 'limit') {
    limitCents = parseDollars(form.limitPrice)
    if (form.limitPrice.trim() === '') errors.limitPrice = 'Enter your limit price.'
    else if (limitCents === null || limitCents <= 0) {
      errors.limitPrice = 'Enter a price in dollars and cents, above zero.'
      limitCents = null
    }
  }
  const hint = entryHintCents(form.limitPolicy, limitCents, ctx.quote)

  const stopCents = parseDollars(form.stopPrice)
  if (form.stopPrice.trim() === '') errors.stopPrice = 'Enter the stop price.'
  else if (stopCents === null || stopCents <= 0) errors.stopPrice = 'Enter a price in dollars and cents, above zero.'
  else if (hint !== null && stopCents >= hint) errors.stopPrice = 'The stop must be below the entry price.'

  let targetCents: number | null = null
  if (form.targetPrice.trim() !== '') {
    targetCents = parseDollars(form.targetPrice)
    if (targetCents === null || targetCents <= 0) errors.targetPrice = 'Enter a price in dollars and cents, or leave it blank.'
    else if (hint !== null && targetCents <= hint) errors.targetPrice = 'The target must be above the entry price.'
    else if (stopCents !== null && targetCents <= stopCents) errors.targetPrice = 'The target must be above the stop.'
  }

  const deadline = form.deadline.trim()
  if (deadline === '') errors.deadline = 'Choose the last session the position may be held.'
  else if (!ISO_DATE.test(deadline) || Number.isNaN(Date.parse(`${deadline}T12:00:00Z`))) {
    errors.deadline = 'Choose a date.'
  } else if (ctx.sessions !== null && !ctx.sessions.includes(deadline)) {
    errors.deadline = 'Choose one of the upcoming market sessions.'
  } else if (deadline < etDate(ctx.now)) errors.deadline = 'The deadline cannot be before today.'
  else if (ctx.maxDeadline != null && deadline > ctx.maxDeadline) {
    const cap = ctx.maxHoldSessions != null ? `at most ${ctx.maxHoldSessions} market sessions` : 'within the hold limit'
    errors.deadline = `Choose a session ${cap} after today: ${ctx.maxDeadline} at the latest.`
  }

  if (Object.keys(errors).length > 0) return { ok: false, errors, entryHintCents: hint }
  const policy: ManualLimitPolicy =
    form.limitPolicy === 'limit' ? { limit_cents: limitCents as number } : form.limitPolicy
  return {
    ok: true,
    entryHintCents: hint,
    ticket: {
      symbol: ctx.symbol,
      quantity,
      stop_price_cents: stopCents as number,
      target_price_cents: targetCents,
      exit_deadline_session: deadline,
      limit_policy: policy,
    },
  }
}
