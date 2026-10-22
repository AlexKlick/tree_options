// Today (Wave 3 lane F): the owner's one-screen desk on desktop and phone,
// read from `GET api/today` (`trex-today/1`).
//
// - Paper only; the owner approves every entry (Review and approve opens the
//   ApprovalSheet, which sends nothing until Approve entry is pressed); exits
//   run automatically.
// - "Pause new entries" is the primary control. It stops entries only; the
//   page says so beside the button. The emergency stop is shown by its
//   label, "Emergency stop (also stops exits)", never offered as the pause.
// - Unknown is never zero: an unavailable block shows its reason words and
//   code (the view's null rule); zero, "" and [] are shown as values.
// - Countdowns run from the moment the response arrived (`secondsLeft`).

import { useState, type ReactNode } from 'react'
import { usePoll } from '../hooks/usePoll'
import {
  CONTROL_LABELS,
  isKnown,
  secondsLeft,
  type CommandKind,
  type GatewayStatus,
  type HumanBlocker,
  type Instant,
  type KillSwitchState,
  type PendingApproval,
  type TodayView,
} from '../lib/contracts'
import { getToday, prepareCommand, serverNowMs, useCommand } from '../lib/commandsApi'
import { ago, etTime } from '../lib/format'
import { formatCents, reasonText } from '../lib/reviewFormat'
import { hashFor } from '../lib/routes'
import { AppShell } from './AppShell'
import { ApprovalSheet, CommandStatusLine, Countdown, ReasonCode, Unavailable, useNow } from './ApprovalSheet'
import './today-review.css'

const SESSION_LABEL = {
  pre_open: 'Before the open',
  open: 'Market open',
  after_close: 'After the close',
  no_session: 'No session today',
} as const
const OWNER_LABEL = { healthy: 'Running', stale: 'Heartbeat stale', stopped: 'Stopped' } as const
const GATEWAY_LABEL: Record<GatewayStatus, string> = {
  ok: 'Connected',
  starting: 'Starting',
  checking: 'Checking',
  needs_login: 'Needs a login',
  needs_2fa: 'Waiting for two-factor approval',
  api_down: 'API not answering',
  down: 'Down',
}
const SEVERITY_ORDER = { critical: 0, warning: 1, info: 2 } as const
const SEVERITY_LABEL = { critical: 'Critical', warning: 'Warning', info: 'Note' } as const
const SCOPE_LABEL: Record<HumanBlocker['scope'], string> = {
  entries: 'Affects new entries',
  exits: 'Affects exits',
  approvals: 'Affects approvals',
  commands: 'Affects commands',
  display: 'Affects this display',
}
/** Few sessions left in the repo calendar means it needs extending soon. */
const CALENDAR_LOW = 20
/** An unconfirmed pause is re-sent as the same request only while it has this long left. */
const RETRY_MARGIN_MS = 5_000

type Tone = 'ok' | 'warn' | 'bad' | 'unknown' | 'neutral'

export function commandLabel(kind: CommandKind): string {
  return (CONTROL_LABELS as Record<string, string>)[kind] ?? kind.replace(/_/g, ' ')
}

const etDay = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' })
const etMonthDay = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric' })

/** `HH:MM ET`, with the ET date first when it is not the view's day. */
function whenEt(at: Instant, viewNow: Instant): string {
  const day = etDay.format(new Date(at)) === etDay.format(new Date(viewNow)) ? '' : `${etMonthDay.format(new Date(at))} `
  return `${day}${etTime(at)} ET`
}

const plural = (n: number, noun: string): string => `${n} ${noun}${n === 1 ? '' : 's'}`

function Tile({ title, tone, children }: { title: string; tone: Tone; children: ReactNode }) {
  return (
    <article className={`status-tile tone-${tone}`}>
      <h3>{title}</h3>
      {children}
    </article>
  )
}

function SessionTile({ view }: { view: TodayView }) {
  const s = view.session
  if (!isKnown(s)) {
    return (
      <Tile title="Market session" tone="unknown">
        <Unavailable reason={s.unavailable_reason} />
      </Tile>
    )
  }
  const current = s.status === 'open' || s.status === 'pre_open'
  return (
    <Tile title="Market session" tone={s.status === 'open' ? 'ok' : 'neutral'}>
      <p className="status-main">{SESSION_LABEL[s.status]}</p>
      <p className="status-sub">
        {current ? 'Session' : 'Next session'} {s.session}: opens {etTime(s.opens_at)} ET, closes {etTime(s.closes_at)} ET
        {s.early_close ? ' (early close)' : ''}
      </p>
      <p className={s.calendar_sessions_left <= CALENDAR_LOW ? 'status-sub warn-text' : 'status-sub muted'}>
        {plural(s.calendar_sessions_left, 'session')} left in the market calendar
      </p>
    </Tile>
  )
}

/** Whether exits run NOW (review P2): the pause never stops them, but the
 * emergency stop does, and so does a desk owner that is not ready. Never a
 * promise beside a standing HALT. */
export function exitsNow(view: TodayView): { running: boolean | null; text: string } {
  if (view.controls.halt.status === 'standing') {
    return { running: false, text: `Exits are NOT running: ${CONTROL_LABELS.halt} is on.` }
  }
  const o = view.desk_owner
  if (!isKnown(o)) return { running: null, text: 'Whether exits are running is unknown.' }
  if (!o.exit_owner_ready) return { running: false, text: 'Exits are NOT running: the desk owner is not ready.' }
  if (view.controls.halt.status === 'unavailable') {
    return { running: null, text: 'Whether exits are running is unknown (the emergency stop cannot be read).' }
  }
  return { running: true, text: 'Exits keep running.' }
}

function OwnerTile({ view }: { view: TodayView }) {
  const o = view.desk_owner
  if (!isKnown(o)) {
    return (
      <Tile title="Desk owner" tone="unknown">
        <Unavailable reason={o.unavailable_reason} />
        <p className="status-sub">Whether exits are watched is unknown.</p>
      </Tile>
    )
  }
  const tone: Tone = !o.exit_owner_ready || o.status === 'stopped' ? 'bad' : o.status === 'stale' ? 'warn' : 'ok'
  const halted = view.controls.halt.status === 'standing'
  return (
    <Tile title="Desk owner" tone={tone}>
      <p className="status-main">{OWNER_LABEL[o.status]}</p>
      <p className="status-sub">
        Heartbeat {ago(o.heartbeat_age_s)} ago (stale after {o.stale_after_s} s);{' '}
        {o.lock_held ? 'holds the desk lock' : 'does not hold the desk lock'}
      </p>
      <p className={o.exit_owner_ready && !halted ? 'status-sub' : 'status-sub bad-text'}>
        {!o.exit_owner_ready
          ? 'Exits are NOT watched'
          : halted
            ? `Exits are watched but NOT sent: ${CONTROL_LABELS.halt} is on`
            : 'Exits are watched'}
      </p>
    </Tile>
  )
}

function GatewayTile({ view }: { view: TodayView }) {
  const g = view.gateway
  if (!isKnown(g)) {
    return (
      <Tile title="IB Gateway (paper)" tone="unknown">
        <Unavailable reason={g.unavailable_reason} />
      </Tile>
    )
  }
  const tone: Tone = g.status === 'ok' ? 'ok' : g.status === 'starting' || g.status === 'checking' ? 'warn' : 'bad'
  return (
    <Tile title="IB Gateway (paper)" tone={tone}>
      <p className="status-main">{GATEWAY_LABEL[g.status]}</p>
      {g.detail && <p className="status-sub">{g.detail}</p>}
      <p className="status-sub muted">Checked {ago(g.age_s)} ago</p>
    </Tile>
  )
}

function EntriesTile({ view }: { view: TodayView }) {
  const p = view.controls.pause_entries
  if (p.status === 'unavailable') {
    return (
      <Tile title="New entries" tone="unknown">
        <Unavailable reason={p.unavailable_reason} />
      </Tile>
    )
  }
  if (p.status === 'standing') {
    const exits = exitsNow(view)
    return (
      <Tile title="New entries" tone="warn">
        <p className="status-main">New entries paused</p>
        <p className={exits.running === false ? 'status-sub bad-text' : 'status-sub'}>
          Since {whenEt(p.since, view.now)}. {exits.text}
        </p>
      </Tile>
    )
  }
  return (
    <Tile title="New entries" tone="ok">
      <p className="status-main">New entries open</p>
      <p className="status-sub">Each entry still needs your approval.</p>
    </Tile>
  )
}

function SwitchRow({ label, state, now }: { label: string; state: KillSwitchState; now: Instant }) {
  return (
    <li className={`switch-row switch-${state.status}`}>
      <span className="switch-label">{label}</span>
      <span className="switch-state">
        {state.status === 'clear' && 'Clear'}
        {state.status === 'standing' && (
          <>
            Standing since {whenEt(state.since, now)}
            {state.reason ? `: ${state.reason}` : ''}
          </>
        )}
        {state.status === 'unavailable' && <Unavailable reason={state.unavailable_reason} />}
      </span>
    </li>
  )
}

function EntriesControl({
  view,
  receivedAtMs,
  onChanged,
}: {
  view: TodayView
  receivedAtMs: number
  onChanged: () => void
}) {
  const runner = useCommand({ onSettled: onChanged })
  const pause = view.controls.pause_entries
  const queued = isKnown(view.controls.queued) ? view.controls.queued.items : null
  const waiting = queued?.find((q) => q.kind === 'pause_entries' || q.kind === 'resume_entries') ?? null
  const standing = pause.status === 'standing'
  const kind = standing ? 'resume_entries' : 'pause_entries'
  const inFlight = runner.phase.name === 'sending' || runner.phase.name === 'waiting'

  const press = () => {
    if (inFlight || waiting !== null) return
    const issuedAt = serverNowMs(view.now, receivedAtMs, Date.now())
    const phase = runner.phase
    // review P2: after an unconfirmed send (network error, 5xx, not JSON) the
    // same press re-sends the SAME request (key and bytes), as the status line
    // promises; only a definite answer, another kind or an expired body
    // (refused command_expired anyway) starts a new command
    if (
      phase.name === 'error' &&
      phase.error.retryable &&
      phase.prepared.kind === kind &&
      Date.parse(phase.prepared.body.expires_at) - RETRY_MARGIN_MS > issuedAt
    ) {
      runner.retry()
      return
    }
    runner.reset()
    runner.submit(prepareCommand(kind, { reason: standing ? 'resumed from Today' : 'paused from Today' }, issuedAt))
  }

  return (
    <div className="entries-control">
      <p className="entries-state">
        {pause.status === 'clear' && 'New entries are open.'}
        {pause.status === 'standing' && (
          <>
            New entries are paused since {whenEt(pause.since, view.now)}
            {pause.reason ? `: ${pause.reason}` : ''}.
          </>
        )}
        {pause.status === 'unavailable' && <Unavailable reason={pause.unavailable_reason} what="Pause state unavailable" />}
      </p>
      <button
        type="button"
        className={standing ? 'secondary-button control-button' : 'primary-button control-button'}
        onClick={press}
        disabled={inFlight || waiting !== null}
      >
        {CONTROL_LABELS[kind]}
      </button>
      <p className="control-note">Pausing stops new entries only; it never stops exits.</p>
      {waiting !== null && (
        <p className="queued-note">
          {commandLabel(waiting.kind)} is queued for the desk owner (by {waiting.issued_by_sub}, expires{' '}
          {whenEt(waiting.expires_at, view.now)}).
        </p>
      )}
      <CommandStatusLine phase={runner.phase} />
    </div>
  )
}

function ControlsCard({
  view,
  receivedAtMs,
  onChanged,
}: {
  view: TodayView
  receivedAtMs: number
  onChanged: () => void
}) {
  const { controls } = view
  return (
    <section className="card today-card" aria-label="Controls">
      <h2>Controls</h2>
      <EntriesControl view={view} receivedAtMs={receivedAtMs} onChanged={onChanged} />
      <h3>Kill switches</h3>
      <ul className="switch-list">
        <SwitchRow label={CONTROL_LABELS.halt} state={controls.halt} now={view.now} />
        <SwitchRow label={CONTROL_LABELS.flatten} state={controls.flatten} now={view.now} />
      </ul>
      <h3>Queued for the desk owner</h3>
      {isKnown(controls.queued) ? (
        controls.queued.items.length === 0 ? (
          <p className="muted">Nothing queued.</p>
        ) : (
          <ul className="queued-list">
            {controls.queued.items.map((q) => (
              <li key={q.idempotency_key}>
                <strong>{commandLabel(q.kind)}</strong> by {q.issued_by_sub}, issued {whenEt(q.issued_at, view.now)},
                expires {whenEt(q.expires_at, view.now)}
              </li>
            ))}
          </ul>
        )
      ) : (
        <Unavailable reason={controls.queued.unavailable_reason} />
      )}
    </section>
  )
}

function ApprovalItem({
  item,
  receivedAtMs,
  now,
  onReview,
}: {
  item: PendingApproval
  receivedAtMs: number
  now: number
  onReview: () => void
}) {
  const left = secondsLeft(item, receivedAtMs, now)
  return (
    <li className="approval-item">
      <div className="approval-head">
        <span className={`kind-badge kind-${item.asset_kind}`}>{item.asset_kind === 'equity' ? 'Equity' : 'Option'}</span>
        <Countdown seconds={left} />
      </div>
      <p className="approval-summary">{item.summary}</p>
      <dl className="facts facts-inline">
        <dt>Max loss</dt>
        <dd>{formatCents(item.max_loss_cents)}</dd>
        <dt>Notional</dt>
        <dd>{formatCents(item.notional_cents)}</dd>
        <dt>Program</dt>
        <dd>{item.program_id}</dd>
      </dl>
      {item.precheck_blockers.length > 0 ? (
        <div className="precheck">
          <p>Advisory pre-checks flag (the desk decides when you approve):</p>
          <ul>
            {item.precheck_blockers.map((code) => (
              <li key={code}>
                {reasonText(code)} <ReasonCode code={code} />
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="muted small">No advisory pre-check flags. The desk checks again when you approve.</p>
      )}
      <div className="approval-actions">
        <button type="button" className="primary-button" onClick={onReview} disabled={left <= 0}>
          Review and approve
        </button>
        <a className="tap-link" href={hashFor('trade-review-proposal', item.proposal_id)}>
          Full details
        </a>
      </div>
    </li>
  )
}

function ApprovalsCard({
  view,
  receivedAtMs,
  now,
  onReview,
}: {
  view: TodayView
  receivedAtMs: number
  now: number
  onReview: (item: PendingApproval) => void
}) {
  const a = view.approvals
  return (
    <section className="card today-card" aria-label="Pending approvals">
      <div className="card-head">
        <h2>Waiting for your approval</h2>
        {isKnown(a) && <span className="count-badge">{a.items.length}</span>}
      </div>
      {!isKnown(a) ? (
        <Unavailable reason={a.unavailable_reason} what="Pending approvals unavailable" />
      ) : (
        <>
          {a.items.length === 0 ? (
            <p className="muted">Nothing is waiting for your approval.</p>
          ) : (
            <ul className="approval-list">
              {a.items.map((item) => (
                <ApprovalItem
                  key={item.proposal_id}
                  item={item}
                  receivedAtMs={receivedAtMs}
                  now={now}
                  onReview={() => onReview(item)}
                />
              ))}
            </ul>
          )}
          {a.problem_count > 0 && (
            <p className="warn-note">
              {plural(a.problem_count, 'proposal file')} could not be read.{' '}
              <a href={hashFor('trade-review')}>Open Trade review</a> to see why.
            </p>
          )}
        </>
      )}
    </section>
  )
}

function BlockersCard({ view }: { view: TodayView }) {
  const sorted = [...view.blockers].sort((x, y) => SEVERITY_ORDER[x.severity] - SEVERITY_ORDER[y.severity])
  return (
    <section className="card today-card" aria-label="Blockers">
      <h2>Blockers</h2>
      {sorted.length === 0 ? (
        <p className="muted">No blockers reported.</p>
      ) : (
        <ul className="blocker-list">
          {sorted.map((b, i) => (
            <li key={`${b.code}-${i}`} className={`blocker severity-${b.severity}`}>
              <p className="blocker-message">
                <span className="severity-label">{SEVERITY_LABEL[b.severity]}</span> {b.message}
              </p>
              {b.next_step && <p className="blocker-next">Next step: {b.next_step}</p>}
              <p className="muted small">
                {SCOPE_LABEL[b.scope]} <ReasonCode code={b.code} />
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function PositionsCard({ view }: { view: TodayView }) {
  const { holdings, risk } = view.positions
  return (
    <section className="card today-card" aria-label="Open positions">
      <h2>Open positions</h2>
      {isKnown(holdings) ? (
        <>
          <p className="positions-count">
            {plural(holdings.option_positions, 'option position')} and{' '}
            {plural(holdings.equity_positions, 'equity position')}
          </p>
          <p className="muted small">
            Holdings as of {whenEt(holdings.as_of, view.now)} ({ago(holdings.age_s)} old)
          </p>
        </>
      ) : (
        <p>
          <Unavailable reason={holdings.unavailable_reason} what="Holdings unavailable" />
        </p>
      )}
      {isKnown(risk) ? (
        <>
          {risk.status === 'stale' && (
            <p className="warn-note">The risk snapshot is stale ({ago(risk.age_s)} old).</p>
          )}
          <dl className="facts">
            <dt>Open loss</dt>
            <dd>{formatCents(risk.open_loss_cents)}</dd>
            <dt>Realized loss today</dt>
            <dd>{formatCents(risk.day_realized_loss_cents)}</dd>
            <dt>Settled cash</dt>
            <dd>{formatCents(risk.settled_cash_cents)}</dd>
            <dt>Positions outside TREX</dt>
            <dd>{risk.outside_positions}</dd>
          </dl>
        </>
      ) : (
        <p>
          <Unavailable reason={risk.unavailable_reason} what="Risk unavailable" />
        </p>
      )}
      <a className="tap-link" href={hashFor('portfolio')}>
        Portfolio details
      </a>
    </section>
  )
}

function Progress({ label, value, of }: { label: string; value: number; of: number }) {
  return (
    <li className="progress-row">
      <span>{label}</span>
      <span className="num">
        {value} of {of}
      </span>
      <progress value={Math.min(value, of)} max={of} aria-label={label} />
    </li>
  )
}

function AcceptanceCard({ view }: { view: TodayView }) {
  const a = view.acceptance
  return (
    <section className="card today-card" aria-label="Paper acceptance">
      <h2>Paper acceptance</h2>
      {isKnown(a) ? (
        <>
          <p className="status-main">{a.verdict === 'met' ? 'Acceptance met' : 'Not met yet'}</p>
          <ul className="progress-list">
            <Progress label="Qualifying sessions in a row" value={a.streak_sessions} of={a.streak_required} />
            <Progress label="Option lifecycles" value={a.option_lifecycles} of={a.lifecycles_required} />
            <Progress label="Equity lifecycles" value={a.equity_lifecycles} of={a.lifecycles_required} />
            <Progress label="Drills passed" value={a.drills_passed} of={a.drills_required} />
          </ul>
          <p className="muted small">Computed {whenEt(a.computed_at, view.now)}.</p>
        </>
      ) : (
        <>
          <p>
            <Unavailable reason={a.unavailable_reason} what="Progress unavailable" />
          </p>
          <p className="muted small">
            Needs {a.streak_required} qualifying sessions in a row, {a.lifecycles_required} completed lifecycles per
            asset kind and all {a.drills_required} drills passed.
          </p>
        </>
      )}
      <p className="live-note">Acceptance never unlocks live trading. Live trading is not authorized.</p>
      <a className="tap-link" href={hashFor('activity-acceptance')}>
        Acceptance details
      </a>
    </section>
  )
}

function EventsCard({ view }: { view: TodayView }) {
  const e = view.next_events
  return (
    <section className="card today-card" aria-label="Coming up">
      <h2>Coming up</h2>
      {!isKnown(e) ? (
        <Unavailable reason={e.unavailable_reason} what="Schedule unavailable" />
      ) : e.items.length === 0 ? (
        <p className="muted">Nothing scheduled.</p>
      ) : (
        <ol className="event-list">
          {e.items.map((event) => (
            <li key={`${event.at}-${event.kind}-${event.subject ?? ''}`}>
              <time dateTime={event.at}>{whenEt(event.at, view.now)}</time>{' '}
              {event.kind === 'approval_deadline' ? (
                <a href={hashFor('trade-review-proposal', event.subject)}>{event.label}</a>
              ) : (
                <span>{event.label}</span>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

interface Selected {
  approval: PendingApproval
  receivedAtMs: number
  responseNow: Instant
}

export function TodayPage() {
  const poll = usePoll(getToday)
  const now = useNow(1000)
  const [selected, setSelected] = useState<Selected | null>(null)
  const view = poll.data
  const receivedAtMs = poll.lastSuccessAt

  const stillPending =
    selected === null ||
    view === null ||
    !isKnown(view.approvals) ||
    view.approvals.items.some((i) => i.proposal_id === selected.approval.proposal_id)

  return (
    <AppShell
      title="Today"
      poll={poll}
      showRuntimeBanners={false}
      contextLabel="trex · paper desk"
      footerSource="Paper trading only. This page queues commands; the desk owner applies them."
    >
      <div className="today-page">
        <p className="paper-note">Paper trading only. You approve every entry; exits run automatically.</p>
        {poll.error && view === null && (
          <p role="alert" className="load-error">
            Today could not be loaded ({poll.error}). Nothing on this page is known until it loads.
          </p>
        )}
        {view === null && poll.error === null && <p className="muted">Loading Today…</p>}
        {view !== null && receivedAtMs !== null && (
          <>
            {poll.error && (
              <p className="warn-note">
                The latest refresh failed ({poll.error}). Showing what arrived at{' '}
                {etTime(new Date(receivedAtMs).toISOString())} ET; countdowns still run from then.
              </p>
            )}
            {view.controls.halt.status === 'standing' && (
              <p className="critical-banner">
                {CONTROL_LABELS.halt} is standing since {whenEt(view.controls.halt.since, view.now)}. Exits are
                stopped too.
              </p>
            )}
            <section className="status-strip" aria-label="Desk status">
              <SessionTile view={view} />
              <OwnerTile view={view} />
              <GatewayTile view={view} />
              <EntriesTile view={view} />
            </section>
            <div className="today-grid">
              <div className="today-col">
                <ApprovalsCard
                  view={view}
                  receivedAtMs={receivedAtMs}
                  now={now}
                  onReview={(approval) => setSelected({ approval, receivedAtMs, responseNow: view.now })}
                />
                <BlockersCard view={view} />
              </div>
              <div className="today-col">
                <ControlsCard view={view} receivedAtMs={receivedAtMs} onChanged={poll.refresh} />
                <PositionsCard view={view} />
                <AcceptanceCard view={view} />
                <EventsCard view={view} />
              </div>
            </div>
          </>
        )}
        {selected !== null && (
          <ApprovalSheet
            key={selected.approval.proposal_id}
            approval={selected.approval}
            receivedAtMs={selected.receivedAtMs}
            responseNow={selected.responseNow}
            mode="dialog"
            closedReason={stillPending ? null : 'This proposal is no longer waiting for approval.'}
            onClose={() => setSelected(null)}
            onSettled={poll.refresh}
          />
        )}
      </div>
    </AppShell>
  )
}
