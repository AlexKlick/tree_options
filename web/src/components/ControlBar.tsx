// The desk control bar, on every page (AppShell). Wave 3 lane E.
//
// Every button QUEUES one `trex-command/1` through POST api/commands (a
// relative URL: it resolves under `/` on loopback and `/trex/` through the
// family portal); the desk owner applies it on its next tick. Nothing here
// talks to a broker, and nothing approves an entry.
//
// - "Pause new entries" is the primary control. It stops entries only;
//   automatic exits keep running.
// - "Emergency stop (also stops exits)", "Close all desk positions" and
//   "Clear emergency stop, close-all and pause" ask for confirmation first.
// - A request whose outcome is unknown (network error, timeout, 5xx, a
//   non-JSON answer) keeps its exact body: pressing the control again or
//   Retry resends the same bytes under the same idempotency key, so the
//   command store answers `duplicate` instead of queueing it twice. Any
//   other command, or a definite answer, drops the kept body, and so does
//   its expiry (a body the store would refuse `command_expired` is never
//   resent; the press mints a fresh command instead).
// - The switch state comes from `GET api/today` (`controls`). Unknown state
//   renders as unavailable with its reason and every control stays offered;
//   it never reads as "allowed".

import { useCallback, useEffect, useRef, useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import {
  API_PATHS,
  CONTROL_LABELS,
  PRIMARY_CONTROL,
  buildCommandBody,
  type CommandResult,
  type Controls,
  type KillSwitchState,
  type TodayView,
} from '../lib/contracts'
import { etTime } from '../lib/format'

export type ControlKind = keyof typeof CONTROL_LABELS

/** Relative to the app's mount (never a leading slash). */
export const COMMANDS_URL = API_PATHS.commands.replace(/^\//, '')
export const TODAY_URL = API_PATHS.today.replace(/^\//, '')
/** The `args.reason` every control-bar command carries (max 500 chars). */
export const CONTROL_REASON = 'from the control bar'
/** The kinds that ask "are you sure" before sending. */
export const CONFIRM_KINDS: ReadonlySet<ControlKind> = new Set<ControlKind>(['halt', 'flatten', 'resume_all'])
const SEND_TIMEOUT_MS = 15_000
const TODAY_POLL_MS = 15_000
/** A kept body is resent only while it has at least this long left. */
const KEEP_MARGIN_MS = 5_000

// ---------------------------------------------------------------- the outbox

export type SendResult =
  | { state: 'answered'; httpStatus: number; result: CommandResult }
  | { state: 'refused'; httpStatus: number; error: string; detail: string }
  | { state: 'unconfirmed'; detail: string }

export interface PendingCommand {
  kind: ControlKind
  key: string
  /** The exact bytes sent; a retry resends them unchanged. */
  body: string
}

export interface OutboxOptions {
  fetch?: typeof fetch
  newKey?: () => string
  timeoutMs?: number
}

/** A random (v4) lower-case UUID; getRandomValues works outside secure contexts too. */
export function newIdempotencyKey(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

const RESULT_STATUSES = new Set(['accepted', 'refused', 'applied', 'failed', 'duplicate'])

function isCommandResult(doc: unknown, key: string): doc is CommandResult {
  if (typeof doc !== 'object' || doc === null) return false
  const d = doc as Record<string, unknown>
  return (
    d.schema === 'trex-command-result/1' &&
    d.idempotency_key === key &&
    typeof d.status === 'string' &&
    RESULT_STATUSES.has(d.status)
  )
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Sends control commands and keeps the one whose outcome is unknown, so a
 * retry reuses its idempotency key and body. One instance per page load
 * (module default below) so the kept body survives navigation.
 */
export class CommandOutbox {
  private kept: PendingCommand | null = null

  constructor(private readonly options: OutboxOptions = {}) {}

  /** The command whose outcome is unknown, if any. */
  pending(): PendingCommand | null {
    return this.kept
  }

  async send(kind: ControlKind, issuedAt: Date): Promise<SendResult> {
    const attempt = this.reusable(kind, issuedAt) ?? this.fresh(kind, issuedAt)
    this.kept = attempt
    const outcome = await this.post(attempt)
    if (outcome.state !== 'unconfirmed' && this.kept === attempt) this.kept = null
    return outcome
  }

  /** The kept body of this kind while it is still valid at `issuedAt` (with
   * KEEP_MARGIN_MS to spare). An expired body would only be refused
   * `command_expired`, costing an extra confirm on the most urgent control
   * (review P2); a fresh key is safe: every control kind is idempotent at
   * the desk, so a stored first attempt plus a second one stops once. */
  private reusable(kind: ControlKind, issuedAt: Date): PendingCommand | null {
    const kept = this.kept
    if (kept === null || kept.kind !== kind) return null
    let expires = Number.NaN
    try {
      expires = Date.parse(String((JSON.parse(kept.body) as { expires_at?: unknown }).expires_at))
    } catch {
      return null
    }
    return Number.isFinite(expires) && expires - KEEP_MARGIN_MS > issuedAt.getTime() ? kept : null
  }

  private fresh(kind: ControlKind, issuedAt: Date): PendingCommand {
    const key = (this.options.newKey ?? newIdempotencyKey)()
    const body = JSON.stringify(buildCommandBody(kind, { reason: CONTROL_REASON }, key, issuedAt))
    return { kind, key, body }
  }

  private async post(attempt: PendingCommand): Promise<SendResult> {
    const send = this.options.fetch ?? ((input: RequestInfo | URL, init?: RequestInit) => fetch(input, init))
    const timeoutMs = this.options.timeoutMs ?? SEND_TIMEOUT_MS
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    let status: number
    let doc: unknown = null
    try {
      const response = await send(COMMANDS_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: attempt.body,
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      })
      status = response.status
      if ((response.headers.get('content-type') ?? '').includes('application/json')) {
        doc = await response.json().catch(() => null)
      }
    } catch (error) {
      const detail = controller.signal.aborted ? `no answer within ${Math.round(timeoutMs / 1000)} s` : errorText(error)
      return { state: 'unconfirmed', detail }
    } finally {
      clearTimeout(timer)
    }
    if ((status === 200 || status === 202 || status === 409) && isCommandResult(doc, attempt.key)) {
      return { state: 'answered', httpStatus: status, result: doc }
    }
    const fields = typeof doc === 'object' && doc !== null ? (doc as Record<string, unknown>) : null
    if (status >= 400 && status < 500 && fields && typeof fields.error === 'string') {
      const detail =
        typeof fields.detail === 'string' ? fields.detail : typeof fields.reason === 'string' ? fields.reason : ''
      return { state: 'refused', httpStatus: status, error: fields.error, detail }
    }
    const error = fields && typeof fields.error === 'string' ? `: ${fields.error}` : ''
    return { state: 'unconfirmed', detail: `HTTP ${status}${error}` }
  }
}

/** The page-wide outbox (survives AppShell remounts on navigation). */
export const defaultOutbox = new CommandOutbox()

// ---------------------------------------------------------------- the view

const EXPLAIN: Record<'halt' | 'flatten' | 'resume_all', string> = {
  halt:
    'This stops new entries AND automatic exits until you clear it: open desk positions stay open with no exit ' +
    'watching them. To stop new entries only, use Pause new entries.',
  flatten:
    'This closes every position the desk owns (paper; options and equities). Positions opened outside the ' +
    'desk are not touched.',
  resume_all:
    'This removes the emergency stop, close-all and the entry pause. Automatic exits run again; every new ' +
    'entry still needs your approval, and earlier approvals are not revived.',
}

type Tone = 'info' | 'ok' | 'warn' | 'error'
interface Message {
  tone: Tone
  text: string
  /** Set when the outcome is unknown: Retry resends this kind's kept body. */
  retry: ControlKind | null
}

function at(instant: string): string {
  return `${etTime(instant)} ET`
}

function describe(kind: ControlKind, outcome: SendResult, kept: PendingCommand | null): Message {
  const label = CONTROL_LABELS[kind]
  if (outcome.state === 'unconfirmed') {
    const key = kept ? ` (key ${kept.key.slice(0, 8)})` : ''
    return {
      tone: 'warn',
      text:
        `Not confirmed: ${label} (${outcome.detail}). Retry sends the same command again${key}, ` +
        'so the desk applies it at most once.',
      retry: kind,
    }
  }
  if (outcome.state === 'refused') {
    const detail = outcome.detail ? `, ${outcome.detail}` : ''
    return {
      tone: 'error',
      text: `Refused: ${label} (${outcome.error}${detail}; HTTP ${outcome.httpStatus}).`,
      retry: null,
    }
  }
  const result = outcome.result
  switch (result.status) {
    case 'accepted':
      return { tone: 'ok', text: `Sent: ${label}. Waiting for the desk to apply it.`, retry: null }
    case 'duplicate': {
      const first = result.result?.first_status
      const outcomeText = typeof first === 'string' ? `first outcome: ${first}` : 'first outcome unavailable'
      return { tone: 'ok', text: `Already received: ${label} (${outcomeText}).`, retry: null }
    }
    case 'refused': {
      const detail = result.detail ? `, ${result.detail}` : ''
      const again = result.reason === 'command_expired' ? ' Press it again to send a new one.' : ''
      return { tone: 'error', text: `Refused: ${label} (${result.reason ?? 'no reason'}${detail}).${again}`, retry: null }
    }
    default:
      return { tone: 'info', text: `${label}: ${result.status}.`, retry: null }
  }
}

type SwitchView = KillSwitchState | { status: 'unavailable'; unavailable_reason: string; since: null; reason: null }

function switchOf(controls: Controls | null, name: 'pause_entries' | 'halt' | 'flatten', why: string): SwitchView {
  return controls ? controls[name] : { status: 'unavailable', unavailable_reason: why, since: null, reason: null }
}

export interface ControlBarViewProps {
  /** `TodayView.controls`, or null when the Today view is unavailable. */
  controls: Controls | null
  /** Why `controls` is null (shown to the owner); 'checking' before the first answer. */
  unavailableReason: string | null
  outbox?: CommandOutbox
  /** Called after the backend answered a command (refresh the state). */
  onSent?: () => void
  /** The clock commands are stamped with (the server's, when known). */
  now?: () => Date
  /** A note about the state's age (last known, refresh failing). */
  stateNote?: string | null
  /** Phone layout (review P2): the entry control and any "clear" stay in
   * view; the emergency stop and close-all sit behind "More controls", so
   * the page's own content (Today's approvals) starts on the first screen. */
  compact?: boolean
}

export function ControlBarView({
  controls,
  unavailableReason,
  outbox = defaultOutbox,
  onSent,
  now = () => new Date(),
  stateNote = null,
  compact = false,
}: ControlBarViewProps) {
  const [confirming, setConfirming] = useState<ControlKind | null>(null)
  const [inFlight, setInFlight] = useState<ControlKind | null>(null)
  const [message, setMessage] = useState<Message | null>(null)
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const why = unavailableReason ?? 'unknown'
  const checking = controls === null && unavailableReason === 'checking'
  const pause = switchOf(controls, 'pause_entries', why)
  const halt = switchOf(controls, 'halt', why)
  const flatten = switchOf(controls, 'flatten', why)

  const send = async (kind: ControlKind) => {
    setConfirming(null)
    setInFlight(kind)
    setMessage({ tone: 'info', text: `Sending: ${CONTROL_LABELS[kind]}...`, retry: null })
    const outcome = await outbox.send(kind, now())
    if (!alive.current) return
    setInFlight(null)
    setMessage(describe(kind, outcome, outbox.pending()))
    if (outcome.state === 'answered') onSent?.()
  }

  const press = (kind: ControlKind) => {
    if (CONFIRM_KINDS.has(kind)) {
      setConfirming(kind)
      return
    }
    void send(kind)
  }

  // What to offer: unknown state offers both directions; never guess "clear".
  const entryKinds: ControlKind[] =
    pause.status === 'clear' ? [PRIMARY_CONTROL] : pause.status === 'standing' ? ['resume_entries'] : [PRIMARY_CONTROL, 'resume_entries']
  const offered: ControlKind[] = [...entryKinds]
  if (halt.status !== 'standing') offered.push('halt')
  if (flatten.status !== 'standing') offered.push('flatten')
  if (halt.status !== 'clear' || flatten.status !== 'clear') offered.push('resume_all')

  const busy = inFlight !== null
  const buttonClass = (kind: ControlKind): string => {
    if (kind === entryKinds[0]) return 'control-button control-primary'
    if (kind === 'halt' || kind === 'flatten') return 'control-button control-danger'
    return 'control-button control-secondary'
  }
  // compact (phone): the stops that are not standing go behind a disclosure
  const tucked = (kind: ControlKind): boolean => compact && (kind === 'halt' || kind === 'flatten')
  const shown = offered.filter((kind) => !tucked(kind))
  const more = offered.filter(tucked)
  const button = (kind: ControlKind) => (
    <button
      key={kind}
      type="button"
      className={buttonClass(kind)}
      disabled={busy}
      aria-expanded={CONFIRM_KINDS.has(kind) ? confirming === kind : undefined}
      onClick={() => press(kind)}
    >
      {CONTROL_LABELS[kind]}
    </button>
  )

  const lines: string[] = []
  if (checking) {
    lines.push('New entries: checking...')
  } else {
    // a clear pause is not "allowed" while another stop blocks every entry (review P2)
    if (pause.status === 'clear' && halt.status === 'standing') lines.push('New entries: blocked by the emergency stop')
    else if (pause.status === 'clear' && flatten.status === 'standing') lines.push('New entries: blocked by close-all')
    else if (pause.status === 'clear') lines.push('New entries: allowed (each one still needs your approval)')
    else if (pause.status === 'standing') lines.push(`New entries: paused since ${at(pause.since)}`)
    else lines.push(`New entries: unavailable (${pause.unavailable_reason})`)

    if (halt.status === 'standing') lines.push(`Emergency stop: ON since ${at(halt.since)}; exits are stopped too.`)
    if (flatten.status === 'standing') {
      const blocked = halt.status === 'standing' ? ' Nothing closes while the emergency stop stands.' : ''
      lines.push(`Close-all: standing since ${at(flatten.since)}.${blocked}`)
    }
    if (halt.status === 'unavailable' && flatten.status === 'unavailable' && halt.unavailable_reason === flatten.unavailable_reason) {
      lines.push(`Emergency stop and close-all: unavailable (${halt.unavailable_reason})`)
    } else {
      if (halt.status === 'unavailable') lines.push(`Emergency stop: unavailable (${halt.unavailable_reason})`)
      if (flatten.status === 'unavailable') lines.push(`Close-all: unavailable (${flatten.unavailable_reason})`)
    }
    if (controls) {
      const queued = controls.queued
      if (queued.status === 'unavailable') {
        lines.push(`Queued commands: unavailable (${queued.unavailable_reason})`)
      } else if (queued.items.length > 0) {
        const names = queued.items.map((item) => {
          const label = (CONTROL_LABELS as Record<string, string>)[item.kind] ?? item.kind
          return `${label} (sent ${at(item.issued_at)})`
        })
        lines.push(`Waiting for the desk: ${names.join(', ')}`)
      }
    }
  }

  return (
    <section className="control-bar" aria-label="Desk controls">
      <div className="control-state">
        {lines.map((line) => (
          <p key={line} className="control-line">
            {line}
          </p>
        ))}
        {stateNote ? <p className="control-line muted">{stateNote}</p> : null}
      </div>
      <div className="control-actions">
        {shown.map(button)}
      </div>
      {more.length > 0 ? (
        <details className="control-more">
          <summary>More controls</summary>
          <div className="control-actions">{more.map(button)}</div>
        </details>
      ) : null}
      <p className="control-hint muted">Pausing stops new entries only; it never stops exits.</p>
      {confirming !== null && confirming !== 'pause_entries' && confirming !== 'resume_entries' ? (
        <div className="control-confirm" role="group" aria-label={`Confirm: ${CONTROL_LABELS[confirming]}`}>
          <p>
            {EXPLAIN[confirming]}
            {confirming === 'flatten' && halt.status === 'standing'
              ? ' Nothing closes while the emergency stop stands: clear it first, then close all.'
              : ''}
          </p>
          <div className="control-actions">
            <button type="button" className="control-button control-danger" disabled={busy} onClick={() => void send(confirming)}>
              Yes: {CONTROL_LABELS[confirming]}
            </button>
            <button type="button" className="control-button" autoFocus onClick={() => setConfirming(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {message ? (
        <div className={`control-message control-${message.tone}`}>
          <p role="status">{message.text}</p>
          {message.retry !== null ? (
            <button
              type="button"
              className="control-button control-secondary"
              disabled={busy}
              onClick={() => void send(message.retry as ControlKind)}
            >
              Retry: {CONTROL_LABELS[message.retry]}
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

// ---------------------------------------------------------------- connected

function isToday(doc: unknown): doc is TodayView {
  if (typeof doc !== 'object' || doc === null) return false
  const d = doc as Record<string, unknown>
  const controls = d.controls as Record<string, { status?: unknown } | undefined> | undefined
  return (
    d.schema === 'trex-today/1' &&
    typeof d.now === 'string' &&
    typeof controls === 'object' &&
    controls !== null &&
    ['pause_entries', 'halt', 'flatten', 'queued'].every((name) => typeof controls[name]?.status === 'string')
  )
}

async function fetchToday(): Promise<TodayView> {
  const response = await fetch(TODAY_URL, {
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
    cache: 'no-store',
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  if (!(response.headers.get('content-type') ?? '').includes('application/json')) throw new Error('not JSON')
  const doc: unknown = await response.json()
  if (!isToday(doc)) throw new Error('not a trex-today/1 document')
  return doc
}

/** The control bar fed by `GET api/today`; commands carry the server's clock. */
export function ControlBar({ outbox = defaultOutbox, compact = false }: { outbox?: CommandOutbox; compact?: boolean }) {
  const poll = usePoll(fetchToday, TODAY_POLL_MS)
  const today = poll.data
  const serverMs = today ? Date.parse(today.now) : Number.NaN
  // server minus device clock at receipt; a skewed phone still stamps commands
  // the desk accepts (C3.2 countdown rule: count from receipt)
  const offsetMs = Number.isFinite(serverMs) && poll.lastSuccessAt !== null ? serverMs - poll.lastSuccessAt : 0
  const now = useCallback(() => new Date(Date.now() + offsetMs), [offsetMs])
  const refresh = poll.refresh
  const unavailableReason = today
    ? null
    : poll.error !== null
      ? `today view: ${poll.error}`
      : 'checking'
  const stateNote = today && poll.error !== null ? `Last known at ${at(today.now)}; refresh failing (${poll.error}).` : null
  return (
    <ControlBarView
      controls={today ? today.controls : null}
      unavailableReason={unavailableReason}
      outbox={outbox}
      onSent={refresh}
      now={now}
      stateNote={stateNote}
      compact={compact}
    />
  )
}
