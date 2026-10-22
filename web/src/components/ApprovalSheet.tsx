// The owner's approve / reject decision on one pending paper entry (Wave 3
// lane F). Used as a dialog from Today and inline on a Trade review detail.
//
// - The owner approves every entry: nothing is sent until the owner presses
//   Approve entry (or confirms a rejection); the sheet never acts on mount.
// - The countdown runs from the moment the response arrived (`secondsLeft`),
//   never from this device's clock against `send_deadline`; at zero both
//   decisions are disabled.
// - One press is one request: `prepareCommand` freezes the body, and "Try
//   again (same request)" re-sends the same bytes under the same key.
// - The command's outcome is read back from `GET api/commands/{key}` until
//   the desk owner applies or refuses it; refusals show the vocabulary words
//   beside the code.

import { useEffect, useId, useRef, useState } from 'react'
import { secondsLeft, type CommandKind, type CommandResult, type Instant, type PendingApproval } from '../lib/contracts'
import { approvalTtlS, prepareCommand, serverNowMs, useCommand, type CommandPhase } from '../lib/commandsApi'
import { etTime } from '../lib/format'
import { formatCents, formatCountdown, reasonText } from '../lib/reviewFormat'
import './today-review.css'

/** This device's clock, ticking every `intervalMs` (drives countdowns only). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(id)
  }, [intervalMs])
  return now
}

export function Countdown({ seconds }: { seconds: number }) {
  const tone = seconds <= 0 ? ' countdown-expired' : seconds <= 60 ? ' countdown-low' : ''
  return (
    <span role="timer" className={`countdown${tone}`}>
      {seconds > 0 ? `${formatCountdown(seconds)} left` : 'Expired'}
    </span>
  )
}

export function ReasonCode({ code }: { code: string }) {
  return <code className="reason-code">{code}</code>
}

/** An unknown value: the words for its reason, with the code beside them. */
export function Unavailable({ reason, what = 'Unavailable' }: { reason: string; what?: string }) {
  return (
    <span className="unavailable">
      {what}: {reasonText(reason)} <ReasonCode code={reason} />
    </span>
  )
}

const APPLIED_TEXT: Partial<Record<CommandKind, string>> = {
  approve_entry: 'Approved. The desk owner granted this one paper entry.',
  reject_entry: 'Rejected. The desk will not send this entry.',
  pause_entries: 'New entries are paused. Pausing never stops exits.',
  resume_entries: 'New entries are open again.',
}

function resultStrings(result: CommandResult, key: string): string[] {
  const value = result.result?.[key]
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []
}

function Codes({ codes }: { codes: string[] }) {
  return (
    <>
      {codes.map((code, i) => (
        <span key={code}>
          {i > 0 ? ' / ' : ''}
          <ReasonCode code={code} />
        </span>
      ))}
    </>
  )
}

function StatusBody({ phase }: { phase: CommandPhase }) {
  switch (phase.name) {
    case 'idle':
      return null
    case 'sending':
      return <>{phase.attempt > 1 ? 'Sending again with the same request key…' : 'Sending…'}</>
    case 'waiting':
      return (
        <>
          Queued for the desk owner. Waiting for it to apply request{' '}
          <code className="hash">{phase.prepared.key.slice(0, 8)}</code>.
        </>
      )
    case 'unconfirmed':
      return (
        <>
          No result from the desk owner yet. The request expired at {etTime(phase.prepared.body.expires_at)} ET; check
          Activity, or check again.
        </>
      )
    case 'error': {
      const { error } = phase
      const codes = [error.code, ...(error.cause ? [error.cause] : [])]
      const words = reasonText(error.cause ?? error.code)
      return (
        <>
          {error.retryable ? 'Not confirmed' : 'TREX refused the request'}: {words} <Codes codes={codes} />
          {error.http !== null ? ` (HTTP ${error.http})` : ''}.
          {error.detail ? ` ${error.detail}.` : ''}
          {error.retryable ? ' Trying again re-sends the same request, so it cannot be applied twice.' : ''}
        </>
      )
    }
    case 'settled': {
      const { result } = phase
      if (result.status === 'applied') {
        const until = typeof result.result?.expires_at === 'string' ? result.result.expires_at : null
        return (
          <>
            {APPLIED_TEXT[result.kind] ?? 'Applied by the desk owner.'}
            {until ? ` It holds until ${etTime(until)} ET.` : ''}
          </>
        )
      }
      const reason = result.reason ?? 'apply_error'
      const others = resultStrings(result, 'blockers').filter((b) => b !== reason)
      return (
        <>
          {result.status === 'refused' ? 'Refused' : 'Failed while applying'}: {reasonText(reason)}{' '}
          <ReasonCode code={reason} />.{result.detail ? ` ${result.detail}.` : ''}
          {others.length > 0 && (
            <ul className="status-blockers">
              {others.map((code) => (
                <li key={code}>
                  {reasonText(code)} <ReasonCode code={code} />
                </li>
              ))}
            </ul>
          )}
        </>
      )
    }
  }
}

function phaseTone(phase: CommandPhase): string {
  if (phase.name === 'settled') return phase.result.status === 'applied' ? 'ok' : 'bad'
  if (phase.name === 'error') return 'bad'
  if (phase.name === 'unconfirmed') return 'warn'
  return 'neutral'
}

/** The live outcome of one command (an always-present polite live region). */
export function CommandStatusLine({ phase }: { phase: CommandPhase }) {
  return (
    <div role="status" aria-live="polite" className={`command-status command-status-${phaseTone(phase)}`}>
      <StatusBody phase={phase} />
    </div>
  )
}

/** What the sheet needs from a Today `PendingApproval` (or a proposal row). */
export type ApprovalFacts = Pick<
  PendingApproval,
  | 'proposal_id'
  | 'summary'
  | 'asset_kind'
  | 'max_loss_cents'
  | 'notional_cents'
  | 'send_deadline'
  | 'seconds_to_deadline'
  | 'approve_args'
>

export interface ApprovalSheetProps {
  approval: ApprovalFacts
  /** When the response carrying `approval` arrived (this device's clock). */
  receivedAtMs: number
  /** That response's `now` (the desk's clock); commands are issued from it. */
  responseNow: Instant
  mode?: 'dialog' | 'inline'
  /** Why no decision can be taken any more (e.g. no longer pending). */
  closedReason?: string | null
  onClose?: () => void
  onSettled?: (result: CommandResult) => void
  pollMs?: number
}

/** The contract's `secondsLeft` for ApprovalFacts (it takes any value with
 * `seconds_to_deadline` since the integration amendment; no cast). */
export const approvalSecondsLeft = (approval: ApprovalFacts, receivedAtMs: number, nowMs: number): number =>
  secondsLeft(approval, receivedAtMs, nowMs)
const leftOn = approvalSecondsLeft

export function ApprovalSheet({
  approval,
  receivedAtMs,
  responseNow,
  mode = 'inline',
  closedReason = null,
  onClose,
  onSettled,
  pollMs,
}: ApprovalSheetProps) {
  const now = useNow(1000)
  const runner = useCommand({ pollMs, onSettled })
  const { phase } = runner
  const [rejecting, setRejecting] = useState(false)
  const [why, setWhy] = useState('')
  const titleId = useId()
  const whyId = useId()
  const heading = useRef<HTMLHeadingElement>(null)

  useEffect(() => {
    if (mode === 'dialog') heading.current?.focus()
  }, [mode])

  const left = leftOn(approval, receivedAtMs, now)
  const expired = left <= 0
  const idle = phase.name === 'idle'
  const canDecide = idle && !expired && closedReason === null

  const issue = (): { issuedAtMs: number; leftNow: number } | null => {
    const clientNow = Date.now()
    const leftNow = leftOn(approval, receivedAtMs, clientNow)
    if (phase.name !== 'idle' || closedReason !== null || leftNow < 1) return null
    return { issuedAtMs: serverNowMs(responseNow, receivedAtMs, clientNow), leftNow }
  }

  const approve = () => {
    const at = issue()
    if (at === null) return
    runner.submit(prepareCommand('approve_entry', { ...approval.approve_args }, at.issuedAtMs, approvalTtlS(at.leftNow)))
  }

  const reject = () => {
    const at = issue()
    if (at === null) return
    const reason = why.trim()
    const args = reason ? { proposal_id: approval.proposal_id, reason } : { proposal_id: approval.proposal_id }
    runner.submit(prepareCommand('reject_entry', args, at.issuedAtMs))
    setRejecting(false)
  }

  const closable = phase.name !== 'sending'
  const close = () => {
    if (closable) onClose?.()
  }
  const startOver =
    (phase.name === 'settled' && phase.result.status !== 'applied') ||
    (phase.name === 'error' && !phase.error.retryable)
  const args = approval.approve_args

  const sheet = (
    <section
      className={`approval-sheet approval-sheet-${mode}`}
      role={mode === 'dialog' ? 'dialog' : 'region'}
      aria-modal={mode === 'dialog' ? true : undefined}
      aria-labelledby={titleId}
      onKeyDown={(e) => {
        if (mode === 'dialog' && e.key === 'Escape') close()
      }}
      onClick={mode === 'dialog' ? (e) => e.stopPropagation() : undefined}
    >
      <div className="sheet-head">
        <h2 id={titleId} ref={heading} tabIndex={-1}>
          {mode === 'dialog' ? 'Approve this paper entry?' : 'Your decision'}
        </h2>
        <Countdown seconds={left} />
      </div>
      <p className="sheet-summary">{approval.summary}</p>
      <dl className="facts">
        <dt>Max loss</dt>
        <dd>{formatCents(approval.max_loss_cents)}</dd>
        <dt>Notional</dt>
        <dd>{formatCents(approval.notional_cents)}</dd>
        <dt>Approve by</dt>
        <dd>{etTime(approval.send_deadline)} ET</dd>
      </dl>
      <p className="sheet-note">
        Paper account only. Approving lets the desk send this one entry; its exits then run automatically. The desk
        checks everything again before it sends.
      </p>
      {idle && closedReason !== null && <p className="sheet-closed">{closedReason}</p>}
      {idle && closedReason === null && expired && (
        <p className="sheet-closed">The approval window closed at {etTime(approval.send_deadline)} ET.</p>
      )}
      {rejecting && canDecide ? (
        <div className="reject-form">
          <label htmlFor={whyId}>Why (optional)</label>
          <textarea id={whyId} maxLength={500} rows={2} value={why} onChange={(e) => setWhy(e.target.value)} />
          <div className="sheet-actions">
            <button type="button" className="danger-button" onClick={reject} disabled={!canDecide}>
              Confirm rejection
            </button>
            <button type="button" className="secondary-button" onClick={() => setRejecting(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="sheet-actions sheet-decision">
          <button type="button" className="primary-button" onClick={approve} disabled={!canDecide}>
            Approve entry
          </button>
          <button type="button" className="secondary-button" onClick={() => setRejecting(true)} disabled={!canDecide}>
            Reject
          </button>
        </div>
      )}
      <CommandStatusLine phase={phase} />
      {(phase.name === 'error' || phase.name === 'unconfirmed' || startOver || mode === 'dialog') && (
        <div className="sheet-actions">
          {phase.name === 'error' && phase.error.retryable && (
            <button type="button" className="secondary-button" onClick={runner.retry} disabled={expired}>
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
          {mode === 'dialog' && (
            <button type="button" className="secondary-button" onClick={close} disabled={!closable}>
              Close
            </button>
          )}
        </div>
      )}
      <details className="sheet-binds">
        <summary>What this approval binds</summary>
        <dl className="facts">
          <dt>Proposal</dt>
          <dd className="hash">{args.proposal_id}</dd>
          <dt>Proposal file hash</dt>
          <dd className="hash">{args.proposal_sha256}</dd>
          <dt>Order hash</dt>
          <dd className="hash">{args.effect_sha256}</dd>
          <dt>Owner epoch</dt>
          <dd className="hash">{args.owner_epoch}</dd>
          <dt>Risk profile digest</dt>
          <dd className="hash">{args.profile_digest}</dd>
        </dl>
      </details>
    </section>
  )

  if (mode !== 'dialog') return sheet
  return (
    <div className="sheet-backdrop" onClick={close}>
      {sheet}
    </div>
  )
}
