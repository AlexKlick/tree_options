// Owner-initiated entries on Trade review (Wave 5 lane web): the Ideas the
// desk qualified and the New SPY trade ticket. Both only ever queue ONE
// `compose_entry`: the desk owner screens it with live facts and writes a
// proposal. Composing never approves and never sends; once the desk answers,
// the owner opens the new proposal and approves it there, as a separate act,
// with its own countdown (TradeReviewPage's ApprovalSheet).
//
// - One press is one request (`prepareCompose` freezes key, time and bytes);
//   "Try again (same request)" re-sends those bytes, so a retry can only be
//   the store's duplicate.
// - Commands are issued at the desk's clock (the ideas response's `now`, or
//   the proposal list's when the ideas list is unavailable).
// - The server's hints are advisory and shown in words; the desk is the
//   judge, and its refusals show their reason words beside the code. Nothing
//   is guessed or hidden. Phone first (390 px), two columns from 960 px.

import { useId, useState, type FormEvent, type ReactNode } from 'react'
import type { PollState } from '../hooks/usePoll'
import { COMMAND_MAX_TTL_S, secondsLeft, type CommandResult, type Instant, type IsoDate } from '../lib/contracts'
import { newIdempotencyKey, secondsUntil, serverNowMs, useCommand, type CommandRunner } from '../lib/commandsApi'
import {
  DEFAULT_EQUITY_SYMBOLS,
  EMPTY_TICKET,
  LIMIT_POLICIES,
  MANUAL_PROGRAM_ID,
  composeArgsForIdea,
  composeArgsForTicket,
  composeTtlS,
  composedProposalId,
  deadlineSessions,
  entryWindowState,
  etDate,
  ideaPreparable,
  prepareCompose,
  validateTicket,
  type IdeaRow,
  type IdeasView,
  type LimitPolicy,
  type ManualEquityTicket,
  type ManualTicketTerms,
  type QuoteFacts,
  type TicketErrors,
  type TicketField,
  type TicketForm,
} from '../lib/ideasApi'
import { etTime } from '../lib/format'
import { formatCents, reasonText } from '../lib/reviewFormat'
import { hashFor } from '../lib/routes'
import { CommandStatusLine, Countdown, ReasonCode, Unavailable, useNow } from './ApprovalSheet'
import './today-review.css'
import './trade-ideas.css'

/** A desk clock reading: a response's `now` and when it arrived here. */
export interface DeskClock {
  now: Instant
  receivedAtMs: number
}

function clockOf(poll: PollState<{ now: Instant }>): DeskClock | null {
  return poll.data !== null && poll.lastSuccessAt !== null ? { now: poll.data.now, receivedAtMs: poll.lastSuccessAt } : null
}

const IDEA_STATE: Record<string, string> = {
  proposed: 'A proposal for it is waiting',
  used: 'Already used: its entry was sent',
  expired: 'Expired',
}

function ReasonList({ codes, className = 'hint-list' }: { codes: string[]; className?: string }) {
  return (
    <ul className={className}>
      {codes.map((code) => (
        <li key={code}>
          {reasonText(code)} <ReasonCode code={code} />
        </li>
      ))}
    </ul>
  )
}

// ---------------------------------------------------------------- one compose

/** The outcome of one `compose_entry`: the live status line, then (once the
 * desk applied it) the link to the new proposal, and the follow-up buttons. */
function ComposeOutcome({ runner, after }: { runner: CommandRunner; after?: ReactNode }) {
  const { phase } = runner
  const settled = phase.name === 'settled' ? phase.result : null
  const proposalId = settled ? composedProposalId(settled) : null
  const deadline = settled && typeof settled.result?.send_deadline === 'string' ? settled.result.send_deadline : null
  const startOver =
    (settled !== null && settled.status !== 'applied') ||
    (phase.name === 'error' && !phase.error.retryable) ||
    phase.name === 'unconfirmed'
  return (
    <div className="compose-outcome">
      <CommandStatusLine phase={phase} />
      {settled?.status === 'applied' &&
        (proposalId !== null ? (
          <p className="compose-ready">
            <a className="tap-link compose-link" href={hashFor('trade-review-proposal', proposalId)}>
              Review and approve the proposal
            </a>
            <span className="muted small">
              {deadline ? `Approve it on its page before ${etTime(deadline)} ET. ` : ''}It is also listed under Waiting for
              your decision.
            </span>
          </p>
        ) : (
          <p className="warn-note">The desk answered without naming the proposal; look for it under Waiting for your decision.</p>
        ))}
      <div className="sheet-actions compose-actions">
        {phase.name === 'error' && phase.error.retryable && (
          <button type="button" className="secondary-button" onClick={runner.retry}>
            Try again (same request)
          </button>
        )}
        {phase.name === 'unconfirmed' && (
          <button type="button" className="secondary-button" onClick={runner.checkAgain}>
            Check again
          </button>
        )}
        {startOver && (
          <button type="button" className="secondary-button" onClick={runner.reset}>
            Start over
          </button>
        )}
        {after}
      </div>
    </div>
  )
}

function useCompose(onComposed: () => void): CommandRunner {
  return useCommand({
    onSettled: (result: CommandResult) => {
      if (result.status === 'applied') onComposed()
    },
  })
}

// ---------------------------------------------------------------- ideas

function IdeaItem({
  idea,
  clock,
  nowMs,
  listed,
  onComposed,
  onPressed,
}: {
  idea: IdeaRow
  clock: DeskClock
  nowMs: number
  listed: boolean
  onComposed: () => void
  onPressed: (idea: IdeaRow) => void
}) {
  const runner = useCompose(onComposed)
  const idle = runner.phase.name === 'idle'
  // the server's own count at its `now`, then counted down from receipt
  const atNow = idea.seconds_to_expiry ?? (idea.valid_until === null ? null : secondsUntil(idea.valid_until, clock.now))
  const leftAt = (ms: number): number | null =>
    atNow === null ? null : secondsLeft({ seconds_to_deadline: atNow }, clock.receivedAtMs, ms)
  const left = leftAt(nowMs)
  const preparable = listed && ideaPreparable(idea, left)

  const prepare = () => {
    const clientNow = Date.now()
    const leftNow = leftAt(clientNow)
    if (!idle || !listed || !ideaPreparable(idea, leftNow)) return
    onPressed(idea)
    runner.submit(
      prepareCompose(composeArgsForIdea(idea), serverNowMs(clock.now, clock.receivedAtMs, clientNow), composeTtlS(leftNow)),
    )
  }

  const money: string[] = []
  if (idea.max_loss_cents !== null) money.push(`max loss ${formatCents(idea.max_loss_cents)}`)
  if (idea.notional_cents !== null) money.push(`notional ${formatCents(idea.notional_cents)}`)
  const expired = idea.status === 'available' && left === 0
  return (
    <li className="idea-item" data-candidate={idea.candidate_id}>
      <div className="idea-head">
        <span className="kind-badge">{idea.asset_kind === 'option' ? 'Option' : 'Stock'}</span>
        <span className="idea-summary">{idea.summary ?? idea.candidate_id}</span>
        {idea.status === 'available' && left !== null && <Countdown seconds={left} />}
      </div>
      <p className="idea-facts muted small">
        {money.length > 0 ? `${money.join(', ')}. ` : 'Max loss and notional not stated. '}
        Program {idea.program_id}, candidate <code className="hash">{idea.candidate_id}</code>
        {idea.valid_until !== null ? `, valid until ${etTime(idea.valid_until)} ET` : ''}.
      </p>
      {idea.status !== 'available' && (
        <p className="idea-why">
          {IDEA_STATE[idea.status] ?? `State ${idea.status}`}
          {idea.status === 'proposed' && idea.proposal_id !== null && (
            <>
              :{' '}
              <a className="tap-link" href={hashFor('trade-review-proposal', idea.proposal_id)}>
                review and approve it
              </a>
            </>
          )}
          .
        </p>
      )}
      {expired && <p className="idea-why">Its time window has passed; it can no longer be prepared.</p>}
      {idea.hints.length > 0 && idea.status === 'available' && !expired && (
        <div className="idea-hints">
          <p>The desk would likely refuse it now (advisory; the desk checks again):</p>
          <ReasonList codes={idea.hints} />
        </div>
      )}
      {!listed && idle && <p className="idea-why">No longer in the desk's list of ideas.</p>}
      {idle ? (
        preparable && (
          <button type="button" className="primary-button idea-prepare" onClick={prepare}>
            Prepare proposal
          </button>
        )
      ) : (
        <ComposeOutcome runner={runner} />
      )}
    </li>
  )
}

/** The Ideas section: qualified candidates, each preparable into a proposal. */
export function IdeasSection({ poll, onComposed }: { poll: PollState<IdeasView>; onComposed: () => void }) {
  const nowMs = useNow(1000)
  // an idea pressed this visit stays on screen (with its outcome and link)
  // even when the next list no longer names it
  const [pressed, setPressed] = useState<Record<string, IdeaRow>>({})
  const view = poll.data
  const clock = clockOf(poll)
  const listedIds = new Set(view?.ideas.map((i) => i.candidate_id) ?? [])
  const gone = Object.values(pressed).filter((i) => !listedIds.has(i.candidate_id))
  const rows = [...(view?.ideas ?? []), ...gone]
  const onPressed = (idea: IdeaRow) => setPressed((prev) => ({ ...prev, [idea.candidate_id]: idea }))

  return (
    <section className="card initiate-card" aria-label="Ideas">
      <h2>Ideas</h2>
      <p className="muted small">
        Candidates the desk qualified. Preparing one asks the desk to check it with live prices and write a proposal; it
        never approves or sends anything.
      </p>
      {view === null ? (
        poll.error ? (
          <p role="alert" className="load-error">
            Ideas could not be loaded ({poll.error}). No idea is shown until the list loads.
          </p>
        ) : (
          <p className="muted">Loading ideas…</p>
        )
      ) : (
        <>
          {poll.error && <p className="warn-note">The latest refresh failed ({poll.error}); showing the last list received.</p>}
          {view.status === 'unavailable' && view.unavailable_reason !== null && (
            <p>
              <Unavailable reason={view.unavailable_reason} what="The desk could not list its ideas" />
            </p>
          )}
          {rows.length === 0 && view.status === 'ok' && <p className="muted">No ideas right now.</p>}
          {rows.length > 0 && clock !== null && (
            <ul className="idea-list">
              {rows.map((idea) => (
                <IdeaItem
                  key={idea.candidate_id}
                  idea={idea}
                  clock={clock}
                  nowMs={nowMs}
                  listed={listedIds.has(idea.candidate_id)}
                  onComposed={onComposed}
                  onPressed={onPressed}
                />
              ))}
            </ul>
          )}
          {view.blocked.length > 0 && (
            <div className="idea-blocked">
              <h3>Blocked by the desk</h3>
              <ul>
                {view.blocked.map((b) => (
                  <li key={b.candidate_id}>
                    <code className="hash">{b.candidate_id}</code> ({b.kind.replace(/_/g, ' ')}): {reasonText(b.reason)}{' '}
                    <ReasonCode code={b.reason} />
                  </li>
                ))}
              </ul>
            </div>
          )}
          {view.problems.length > 0 && (
            <ul className="idea-problems small">
              {view.problems.map((p) => (
                <li key={p.file}>
                  <code>{p.file}</code>: {reasonText(p.reason)} <ReasonCode code={p.reason} />
                </li>
              ))}
            </ul>
          )}
          {view.unreadable > 0 && (
            <p className="warn-note">
              {view.unreadable} item{view.unreadable === 1 ? '' : 's'} in the list could not be read by this page.
            </p>
          )}
          {view.truncated && <p className="muted small">Only the newest ideas are listed.</p>}
        </>
      )}
    </section>
  )
}

// ---------------------------------------------------------------- the manual ticket

const POLICY_TEXT: Record<LimitPolicy, { label: string; note: string }> = {
  ask: { label: 'Pay the ask', note: 'The limit is the real-time ask when the desk prepares the proposal.' },
  mid: { label: 'Midpoint', note: 'The limit is halfway between the real-time bid and ask (rounded up to the cent).' },
  limit: { label: 'My limit', note: 'You name the most you will pay per share.' },
}

const sessionLabelFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC',
  weekday: 'short',
  month: 'short',
  day: 'numeric',
})

function sessionLabel(day: IsoDate, today: IsoDate): string {
  const words = sessionLabelFmt.format(new Date(`${day}T12:00:00Z`))
  return day === today ? `${words} (today)` : words
}

function describeTicket(t: ManualEquityTicket): string {
  const p = t.limit_policy
  const entry =
    typeof p === 'object' ? `with a limit of ${formatCents(p.limit_cents)}` : p === 'ask' ? 'at the real-time ask' : 'at the real-time midpoint'
  const target = t.target_price_cents == null ? 'no target' : `target ${formatCents(t.target_price_cents)}`
  return `Buy ${t.quantity} ${t.symbol} ${entry}; stop ${formatCents(t.stop_price_cents)}; ${target}; exit by the end of the ${t.exit_deadline_session} session.`
}

function EntryHint({ policy, hint, quote }: { policy: LimitPolicy; hint: number | null; quote: QuoteFacts | null }) {
  if (policy === 'limit') {
    return <p className="field-hint">The stop must be below your limit and a target above it.</p>
  }
  if (hint === null || quote === null) {
    return (
      <p className="field-hint">
        The desk sets the limit from its own real-time quote when it prepares the proposal, and refuses the ticket if the
        stop is not below it.
      </p>
    )
  }
  return (
    <p className="field-hint">
      Entry price now: about {formatCents(hint)} ({policy === 'ask' ? 'the ask' : 'the midpoint'}; bid{' '}
      {formatCents(quote.bid_cents)}, ask {formatCents(quote.ask_cents)}, {quote.market_data_type} at {etTime(quote.as_of)}{' '}
      ET). The stop must be below it and a target above it.
    </p>
  )
}

function WindowLine({ terms, deskNowMs }: { terms: ManualTicketTerms | null; deskNowMs: number | null }) {
  if (terms === null || deskNowMs === null) {
    return <>Entries are prepared and sent only during regular market hours; the desk refuses outside its entry window.</>
  }
  const state = entryWindowState(terms, deskNowMs)
  const span =
    terms.window_opens_at !== null && terms.window_closes_at !== null
      ? `${etTime(terms.window_opens_at)} to ${etTime(terms.window_closes_at)} ET`
      : null
  switch (state) {
    case 'open':
      return <>Open now{span ? `, ${span}` : ''}; prepared entries must be approved before it closes.</>
    case 'not_yet':
      return <>Opens today{span ? `, ${span}` : ''}; the desk refuses a ticket before then.</>
    case 'closed':
      return <>Closed for today{span ? ` (${span})` : ''}; the desk refuses a ticket until the next session.</>
    case 'no_session':
      return <>Today is not a market session; the desk refuses a ticket until the next one.</>
    default:
      return <>Entries are prepared and sent only during regular market hours; the desk refuses outside its entry window.</>
  }
}

function Field({ id, label, error, children }: { id: string; label: string; error: string | undefined; children: ReactNode }) {
  return (
    <div className="ticket-field">
      <label htmlFor={id}>{label}</label>
      {children}
      {error && (
        <p className="field-error" id={`${id}-error`}>
          {error}
        </p>
      )}
    </div>
  )
}

/** The New SPY trade ticket: one manual whole-share entry, composed by the
 * desk into a proposal the owner then approves on its own page. */
export function ManualTicketSection({
  ideas,
  fallbackClock,
  onComposed,
}: {
  ideas: PollState<IdeasView>
  /** The proposal list's clock, used when the ideas list is unavailable. */
  fallbackClock: DeskClock | null
  onComposed: () => void
}) {
  const runner = useCompose(onComposed)
  const nowMs = useNow(1000)
  const [form, setForm] = useState<TicketForm>(EMPTY_TICKET)
  const [tried, setTried] = useState(false)
  const base = useId()
  const ids = {
    quantity: `${base}-qty`,
    limitPrice: `${base}-limit`,
    stopPrice: `${base}-stop`,
    targetPrice: `${base}-target`,
    deadline: `${base}-deadline`,
  }
  const view = ideas.data
  const clock = clockOf(ideas) ?? fallbackClock
  const terms = view?.manual_equity ?? null
  const symbols = terms?.symbols ?? [...DEFAULT_EQUITY_SYMBOLS]
  const symbol = symbols.includes('SPY') ? 'SPY' : null
  const policies = terms?.limit_policies ?? [...LIMIT_POLICIES]
  const sessions = view ? deadlineSessions(view) : null
  const quote = terms?.quote ?? null
  // the hold cap: the latest exit deadline the desk accepts today
  const maxDeadline = terms?.max_exit_deadline_session ?? null
  const maxHoldSessions = terms?.max_hold_sessions ?? null
  const idle = runner.phase.name === 'idle'

  const deskNowMs = (clientNow: number): number | null =>
    clock === null ? null : serverNowMs(clock.now, clock.receivedAtMs, clientNow)
  const deskNowIso = (clientNow: number): Instant | null => {
    const ms = deskNowMs(clientNow)
    return ms === null ? null : new Date(ms).toISOString()
  }
  const today = clock === null ? null : etDate(clock.now)
  const check =
    clock === null || symbol === null
      ? null
      : validateTicket(form, { now: deskNowIso(nowMs) ?? clock.now, symbol, sessions, quote, maxDeadline, maxHoldSessions })
  const errors: TicketErrors = tried && check !== null && !check.ok ? check.errors : {}
  const canSend = idle && clock !== null && symbol !== null && policies.includes(form.limitPolicy)

  const set = (field: keyof TicketForm) => (value: string) => setForm((prev) => ({ ...prev, [field]: value }))

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!canSend || clock === null || symbol === null) return
    setTried(true)
    const clientNow = Date.now()
    const result = validateTicket(form, {
      now: deskNowIso(clientNow) ?? clock.now,
      symbol,
      sessions,
      quote,
      maxDeadline,
      maxHoldSessions,
    })
    if (!result.ok) return
    const key = newIdempotencyKey()
    const args = composeArgsForTicket(result.ticket, key, terms?.program_id ?? MANUAL_PROGRAM_ID)
    runner.submit(prepareCompose(args, serverNowMs(clock.now, clock.receivedAtMs, clientNow), COMMAND_MAX_TTL_S, key))
  }

  const newTicket = () => {
    runner.reset()
    setForm(EMPTY_TICKET)
    setTried(false)
  }

  const invalid = (field: TicketField) =>
    errors[field] ? { 'aria-invalid': true as const, 'aria-describedby': `${ids[field]}-error` } : {}

  return (
    <section className="card initiate-card" aria-label="New SPY trade">
      <h2>New SPY trade</h2>
      <ul className="ticket-warnings">
        <li>
          <strong>Paper account only.</strong> This trades the IBKR paper account, never real money.
        </li>
        <li>
          <strong>Cash only.</strong> Whole shares bought with settled cash: no margin, no short selling.
        </li>
        <li>
          <strong>Needs a real-time quote.</strong> The desk refuses to prepare an SPY entry on a delayed or frozen quote.
        </li>
        <li>
          <strong>Entry window.</strong> <WindowLine terms={terms} deskNowMs={deskNowMs(nowMs)} />
        </li>
        <li>
          <strong>Preparing never approves or sends.</strong> You approve the proposal as a separate step on its page; its
          exits (stop, target, deadline, a forced exit at 15:45 ET on the last session) then run automatically.
          {terms?.max_entries_per_session != null && ` At most ${terms.max_entries_per_session} entries per session.`}
        </li>
      </ul>
      {view === null && ideas.error && (
        <p className="warn-note">
          The desk's ticket terms could not be loaded ({ideas.error}): the desk checks the symbol, the price and the
          deadline session when it prepares the proposal.
        </p>
      )}
      {terms !== null && terms.hints.length > 0 && (
        <div className="ticket-hints">
          <p>The desk would likely refuse a ticket now (advisory; it checks again when it prepares):</p>
          <ReasonList codes={terms.hints} />
        </div>
      )}
      {symbol === null && (
        <p className="ticket-blocked">
          The desk lists no SPY ticket ({symbols.join(', ') || 'no symbols'}); nothing can be prepared here.
        </p>
      )}
      {clock === null && !ideas.error && <p className="muted">Waiting for the desk clock…</p>}
      <form className="ticket-form" onSubmit={submit} noValidate>
        <fieldset className="ticket-fields" disabled={!idle}>
          <Field id={ids.quantity} label="Shares" error={errors.quantity}>
            <input
              id={ids.quantity}
              inputMode="numeric"
              autoComplete="off"
              value={form.quantity}
              onChange={(e) => set('quantity')(e.target.value)}
              {...invalid('quantity')}
            />
          </Field>
          <fieldset className="ticket-policy">
            <legend>Entry limit</legend>
            {LIMIT_POLICIES.filter((p) => policies.includes(p)).map((policy) => (
              <label key={policy} className="policy-option">
                <input
                  type="radio"
                  name={`${base}-policy`}
                  value={policy}
                  checked={form.limitPolicy === policy}
                  onChange={() => setForm((prev) => ({ ...prev, limitPolicy: policy }))}
                  aria-describedby={`${base}-policy-${policy}`}
                />
                <span className="policy-label">{POLICY_TEXT[policy].label}</span>
                <span className="policy-note" id={`${base}-policy-${policy}`}>
                  {POLICY_TEXT[policy].note}
                </span>
              </label>
            ))}
          </fieldset>
          {form.limitPolicy === 'limit' && (
            <Field id={ids.limitPrice} label="Limit price ($)" error={errors.limitPrice}>
              <input
                id={ids.limitPrice}
                inputMode="decimal"
                autoComplete="off"
                value={form.limitPrice}
                onChange={(e) => set('limitPrice')(e.target.value)}
                {...invalid('limitPrice')}
              />
            </Field>
          )}
          <EntryHint policy={form.limitPolicy} hint={check?.entryHintCents ?? null} quote={quote} />
          <div className="ticket-row">
            <Field id={ids.stopPrice} label="Stop price ($)" error={errors.stopPrice}>
              <input
                id={ids.stopPrice}
                inputMode="decimal"
                autoComplete="off"
                value={form.stopPrice}
                onChange={(e) => set('stopPrice')(e.target.value)}
                {...invalid('stopPrice')}
              />
            </Field>
            <Field id={ids.targetPrice} label="Target price ($, optional)" error={errors.targetPrice}>
              <input
                id={ids.targetPrice}
                inputMode="decimal"
                autoComplete="off"
                value={form.targetPrice}
                onChange={(e) => set('targetPrice')(e.target.value)}
                {...invalid('targetPrice')}
              />
            </Field>
          </div>
          <Field id={ids.deadline} label="Exit by the end of" error={errors.deadline}>
            {sessions !== null ? (
              <select
                id={ids.deadline}
                value={form.deadline}
                onChange={(e) => set('deadline')(e.target.value)}
                {...invalid('deadline')}
              >
                <option value="">Choose a market session</option>
                {sessions.map((day) => (
                  <option key={day} value={day}>
                    {today ? sessionLabel(day, today) : day} ({day})
                  </option>
                ))}
              </select>
            ) : (
              <input
                id={ids.deadline}
                type="date"
                min={today ?? undefined}
                max={maxDeadline ?? undefined}
                value={form.deadline}
                onChange={(e) => set('deadline')(e.target.value)}
                {...invalid('deadline')}
              />
            )}
          </Field>
          {sessions === null && (
            <p className="field-hint">Pick a weekday the market is open; the desk refuses a date that is not a market session.</p>
          )}
          {maxDeadline !== null && (
            <p className="field-hint">
              Hold it at most {maxHoldSessions ?? 'the allowed number of'} market sessions after today: {maxDeadline} at the
              latest.
            </p>
          )}
        </fieldset>
        {idle && check?.ok && <p className="ticket-preview">{describeTicket(check.ticket)}</p>}
        {idle && (
          <button type="submit" className="primary-button ticket-submit" disabled={!canSend}>
            Prepare proposal
          </button>
        )}
      </form>
      {!idle && (
        <ComposeOutcome
          runner={runner}
          after={
            runner.phase.name === 'settled' && runner.phase.result.status === 'applied' ? (
              <button type="button" className="secondary-button" onClick={newTicket}>
                New ticket
              </button>
            ) : undefined
          }
        />
      )}
    </section>
  )
}
