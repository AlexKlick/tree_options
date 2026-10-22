// Trade review (Wave 3 lane F): every proposal the desk owner composed, read
// from `GET api/proposals` (`trex-proposal-list/1`), and one proposal's full
// detail with the owner's approve / reject decision.
//
// - `#/trade-review`: pending proposals first (live countdowns), then the
//   earlier ones with their state, then any proposal file the desk could not
//   read (never silently dropped).
// - `#/trade-review/:proposalId`: the order (effect), quantity, limit, max
//   loss, notional, exit policy, quote type and age, the screening record,
//   the advisory pre-checks from Today, and the hashes, owner epoch and
//   profile digest an approval binds. The ApprovalSheet is inline; it stays
//   mounted once shown so a decision in flight keeps reporting after the row
//   leaves `pending`.
// - An effect schema this page does not know is shown exactly as recorded.
// - Wave 5: the index also lets the owner START an entry (TradeIdeas.tsx):
//   Ideas (`GET api/ideas`) and the New SPY trade ticket each queue one
//   `compose_entry`; the desk's proposal then appears under Waiting for your
//   decision and is approved here as before, never by composing.

import { useEffect, useId, useState, type ReactNode } from 'react'
import { usePoll } from '../hooks/usePoll'
import {
  isKnown,
  type EntryProposalDoc,
  type Instant,
  type ProposalList,
  type ProposalRow,
  type TodayView,
} from '../lib/contracts'
import { approveArgsFromRow, getProposals, getToday, secondsUntil } from '../lib/commandsApi'
import { getIdeas } from '../lib/ideasApi'
import type { PollState } from '../hooks/usePoll'
import { ago, etTime } from '../lib/format'
import { formatCents, formatCountdown, reasonText } from '../lib/reviewFormat'
import { hashFor } from '../lib/routes'
import { AppShell } from './AppShell'
import {
  ApprovalSheet,
  Countdown,
  ReasonCode,
  Unavailable,
  approvalSecondsLeft,
  useNow,
  type ApprovalFacts,
} from './ApprovalSheet'
import { IdeasSection, ManualTicketSection } from './TradeIdeas'
import './today-review.css'

export const STATUS_LABEL: Record<ProposalRow['status'], string> = {
  pending: 'Waiting for your decision',
  approved: 'Approved',
  consumed: 'Approved and used',
  rejected: 'Rejected',
  expired: 'Expired',
  unknown: 'State unknown',
}

// ---------------------------------------------------------------- effect views

// Display-only narrowings of the two effect schemas the desk proposes
// (`supervised-equity-effect/1`, `supervised-effect/1`; Python is the
// source). Each is checked field by field at run time; anything that does
// not fit is shown raw instead of guessed.

interface EquityEffectView {
  schema: 'supervised-equity-effect/1'
  intent_id: string
  account_id: string
  symbol: string
  side: string
  quantity: number
  limit_cents: number
  tif: string
  exit_policy: {
    stop_price_cents: number
    target_price_cents: number | null
    exit_deadline_session: string
    stop_confirm_ticks: number
    forced_exit_et: string
  }
}

interface Threshold {
  basis: string
  value: string
}
interface OptionLegView {
  action: string
  right: string
  strike: string
  expiry: string
  ratio: number
}
interface OptionEffectView {
  schema: 'supervised-effect/1'
  intent_id: string
  account_id: string
  side: string
  quantity: number
  limit: string
  structure: {
    kind: string
    underlying: string
    legs: OptionLegView[]
    quantity: number
    limit: string
    exit_deadline: string
    exits: {
      take_profit: Threshold | null
      stop_loss: Threshold | null
      stop_confirm_ticks: number
      touch: boolean
      breach: boolean
    }
  }
}

type Rec = Record<string, unknown>
const isRec = (v: unknown): v is Rec => typeof v === 'object' && v !== null && !Array.isArray(v)
const isStr = (v: unknown): v is string => typeof v === 'string'
const isInt = (v: unknown): v is number => Number.isInteger(v)
const isThreshold = (v: unknown): v is Threshold | null => v === null || (isRec(v) && isStr(v.basis) && isStr(v.value))

function asEquity(effect: Rec): EquityEffectView | null {
  const p = effect.exit_policy
  const ok =
    effect.schema === 'supervised-equity-effect/1' &&
    isStr(effect.intent_id) &&
    isStr(effect.account_id) &&
    isStr(effect.symbol) &&
    isStr(effect.side) &&
    isInt(effect.quantity) &&
    isInt(effect.limit_cents) &&
    isStr(effect.tif) &&
    isRec(p) &&
    isInt(p.stop_price_cents) &&
    (p.target_price_cents === null || p.target_price_cents === undefined || isInt(p.target_price_cents)) &&
    isStr(p.exit_deadline_session) &&
    isInt(p.stop_confirm_ticks) &&
    isStr(p.forced_exit_et)
  if (!ok) return null
  const view = effect as unknown as EquityEffectView
  return { ...view, exit_policy: { ...view.exit_policy, target_price_cents: view.exit_policy.target_price_cents ?? null } }
}

function asOption(effect: Rec): OptionEffectView | null {
  const s = effect.structure
  if (
    effect.schema !== 'supervised-effect/1' ||
    !isStr(effect.intent_id) ||
    !isStr(effect.account_id) ||
    !isStr(effect.side) ||
    !isInt(effect.quantity) ||
    !isStr(effect.limit) ||
    !isRec(s) ||
    !isStr(s.kind) ||
    !isStr(s.underlying) ||
    !isInt(s.quantity) ||
    !isStr(s.limit) ||
    !isStr(s.exit_deadline) ||
    !Array.isArray(s.legs) ||
    !isRec(s.exits)
  ) {
    return null
  }
  const legsOk = s.legs.every(
    (leg) => isRec(leg) && isStr(leg.action) && isStr(leg.right) && isStr(leg.strike) && isStr(leg.expiry) && isInt(leg.ratio),
  )
  const x = s.exits
  const exitsOk =
    isThreshold(x.take_profit ?? null) &&
    isThreshold(x.stop_loss ?? null) &&
    isInt(x.stop_confirm_ticks) &&
    typeof x.touch === 'boolean' &&
    typeof x.breach === 'boolean'
  return legsOk && exitsOk ? (effect as unknown as OptionEffectView) : null
}

const titleCase = (word: string): string => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()

/** One line naming the order (`Buy 2 SPY`, `Buy 1 SPY debit vertical`). */
export function proposalHeadline(doc: EntryProposalDoc): string {
  const equity = asEquity(doc.effect)
  if (equity) return `${titleCase(equity.side)} ${equity.quantity} ${equity.symbol}`
  const option = asOption(doc.effect)
  if (option) {
    const s = option.structure
    return `${titleCase(option.side)} ${option.quantity} ${s.underlying} ${s.kind.replace(/_/g, ' ')}`
  }
  return `Unrecognized order (${String(doc.effect.schema)})`
}

/** The sheet's facts for a proposal row, counted at the list's `now`. */
export function approvalFromRow(row: ProposalRow, listNow: Instant): ApprovalFacts {
  return {
    proposal_id: row.proposal_id,
    summary: proposalHeadline(row.proposal),
    asset_kind: row.asset_kind,
    max_loss_cents: row.proposal.review.max_loss_cents,
    notional_cents: row.proposal.review.notional_cents,
    send_deadline: row.send_deadline,
    seconds_to_deadline: secondsUntil(row.send_deadline, listNow),
    approve_args: approveArgsFromRow(row),
  }
}

// ---------------------------------------------------------------- fact blocks

function Facts({ children }: { children: ReactNode }) {
  return <dl className="facts">{children}</dl>
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  )
}

const RIGHT = { P: 'Put', C: 'Call' } as Record<string, string>
const TAKE_PROFIT_RULE: Record<string, string> = {
  width_frac: 'package mid at or above value x width',
  gain_frac: 'package mid at or above entry x (1 + value)',
  credit_frac: 'that share of the entry credit captured',
}
const STOP_RULE: Record<string, string> = {
  debit_frac: 'package mid at or below entry x (1 - value)',
  credit_mult: 'cost to close at or above value x the entry credit',
}

function ThresholdText({ t, rules }: { t: Threshold | null; rules: Record<string, string> }) {
  if (t === null) return <>None set</>
  return (
    <>
      {t.basis} {t.value}
      {rules[t.basis] ? ` (${rules[t.basis]})` : ''}
    </>
  )
}

function EquityOrder({ e }: { e: EquityEffectView }) {
  const p = e.exit_policy
  return (
    <>
      <h3>Order</h3>
      <Facts>
        <Fact label="Order">{`${titleCase(e.side)} ${e.quantity} ${e.symbol}`}</Fact>
        <Fact label="Limit price">
          {formatCents(e.limit_cents)} per share ({e.tif}, regular hours only)
        </Fact>
        <Fact label="Paper account">{e.account_id}</Fact>
        <Fact label="Intent">{e.intent_id}</Fact>
      </Facts>
      <h3>Exit policy (automatic)</h3>
      <Facts>
        <Fact label="Stop">
          {formatCents(p.stop_price_cents)}, after {p.stop_confirm_ticks} quote ticks at or below it
        </Fact>
        <Fact label="Target">{p.target_price_cents === null ? 'None set' : formatCents(p.target_price_cents)}</Fact>
        <Fact label="Exit by">
          The {p.exit_deadline_session} session; forced exit after {p.forced_exit_et} ET
        </Fact>
      </Facts>
    </>
  )
}

function OptionOrder({ e }: { e: OptionEffectView }) {
  const s = e.structure
  return (
    <>
      <h3>Order</h3>
      <Facts>
        <Fact label="Order">{`${titleCase(e.side)} ${e.quantity} ${s.underlying} ${s.kind.replace(/_/g, ' ')}`}</Fact>
        <Fact label="Limit price">{e.limit} per share</Fact>
        <Fact label="Structure cap">
          {s.limit} per package, {s.quantity} package{s.quantity === 1 ? '' : 's'} planned
        </Fact>
        <Fact label="Paper account">{e.account_id}</Fact>
        <Fact label="Intent">{e.intent_id}</Fact>
      </Facts>
      <div className="table-scroll">
        <table aria-label="Legs" className="legs-table">
          <thead>
            <tr>
              <th>Action</th>
              <th>Right</th>
              <th className="num">Strike</th>
              <th>Expiry</th>
              <th className="num">Ratio</th>
            </tr>
          </thead>
          <tbody>
            {s.legs.map((leg, i) => (
              <tr key={i}>
                <td>{leg.action}</td>
                <td>{RIGHT[leg.right] ?? leg.right}</td>
                <td className="num">{leg.strike}</td>
                <td>{leg.expiry}</td>
                <td className="num">{leg.ratio}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h3>Exit policy (automatic)</h3>
      <Facts>
        <Fact label="Take profit">
          <ThresholdText t={s.exits.take_profit ?? null} rules={TAKE_PROFIT_RULE} />
        </Fact>
        <Fact label="Stop loss">
          <ThresholdText t={s.exits.stop_loss ?? null} rules={STOP_RULE} />
        </Fact>
        <Fact label="Confirmation">{s.exits.stop_confirm_ticks} quote ticks</Fact>
        <Fact label="Exit by">{s.exit_deadline}</Fact>
        <Fact label="Exit on touch of the long strike">{s.exits.touch ? 'Yes' : 'No'}</Fact>
        <Fact label="Exit on breach of a short strike">{s.exits.breach ? 'Yes' : 'No'}</Fact>
      </Facts>
    </>
  )
}

function OrderFacts({ doc }: { doc: EntryProposalDoc }) {
  const equity = asEquity(doc.effect)
  if (equity) return <EquityOrder e={equity} />
  const option = asOption(doc.effect)
  if (option) return <OptionOrder e={option} />
  return (
    <>
      <h3>Order</h3>
      <p className="warn-note">
        Unrecognized order schema <code>{String(doc.effect.schema)}</code>; shown exactly as recorded.
      </p>
      <pre className="raw-effect">{JSON.stringify(doc.effect, null, 2)}</pre>
    </>
  )
}

function PrecheckFacts({ today, proposalId }: { today: PollState<TodayView>; proposalId: string }) {
  const view = today.data
  if (view === null) {
    return today.error ? (
      <span className="unavailable">Advisory pre-checks unavailable: Today could not be loaded ({today.error})</span>
    ) : (
      <span className="muted">Loading advisory pre-checks…</span>
    )
  }
  if (!isKnown(view.approvals)) {
    return <Unavailable reason={view.approvals.unavailable_reason} what="Advisory pre-checks unavailable" />
  }
  const item = view.approvals.items.find((i) => i.proposal_id === proposalId)
  if (!item) {
    return <span className="unavailable">Advisory pre-checks unavailable: this proposal is not in Today's pending list</span>
  }
  if (item.precheck_blockers.length === 0) {
    return <>No advisory pre-check flags. The desk checks again when you approve.</>
  }
  return (
    <ul className="precheck-list">
      {item.precheck_blockers.map((code) => (
        <li key={code}>
          {reasonText(code)} <ReasonCode code={code} />
        </li>
      ))}
    </ul>
  )
}

function ProposalFacts({
  row,
  list,
  today,
}: {
  row: ProposalRow
  list: ProposalList
  today: PollState<TodayView>
}) {
  const doc = row.proposal
  const review = doc.review
  const quoteAge = Math.round((Date.parse(list.now) - Date.parse(review.quote.as_of)) / 1000)
  return (
    <section className="card review-facts" aria-label="Proposal facts">
      <OrderFacts doc={doc} />
      <h3>Risk</h3>
      <Facts>
        <Fact label="Max loss">{formatCents(review.max_loss_cents)}</Fact>
        <Fact label="Notional">{formatCents(review.notional_cents)}</Fact>
      </Facts>
      <h3>Quote at proposal</h3>
      <Facts>
        <Fact label="Bid / ask">
          {formatCents(review.quote.bid_cents)} / {formatCents(review.quote.ask_cents)}
        </Fact>
        <Fact label="Market data">
          <span className={review.quote.market_data_type === 'realtime' ? '' : 'warn-text'}>
            {review.quote.market_data_type}
          </span>
        </Fact>
        <Fact label="As of">
          {etTime(review.quote.as_of)} ET{' '}
          {quoteAge >= 0 ? (
            `(${ago(quoteAge)} old when listed)`
          ) : (
            <span className="warn-text">(dated after the list time)</span>
          )}
        </Fact>
      </Facts>
      <h3>Screening</h3>
      <Facts>
        <Fact label="Screening record">
          <span className="hash">{review.screening_sha256}</span>
        </Fact>
        <Fact label="Advisory pre-checks">
          <PrecheckFacts today={today} proposalId={row.proposal_id} />
        </Fact>
      </Facts>
      <h3>What an approval binds</h3>
      <Facts>
        <Fact label="Owner epoch">
          <span className="hash">{review.owner_epoch}</span>
        </Fact>
        <Fact label="Risk profile digest">
          <span className="hash">{review.profile_digest}</span>
        </Fact>
        <Fact label="Order hash">
          <span className="hash">{doc.effect_sha256}</span>
        </Fact>
        <Fact label="Proposal file hash">
          <span className="hash">{row.proposal_sha256}</span>
        </Fact>
        <Fact label="Proposal">
          <span className="hash">{row.proposal_id}</span>
        </Fact>
      </Facts>
      <h3>Provenance</h3>
      <Facts>
        <Fact label="Program">{doc.program_id}</Fact>
        <Fact label="Strategy version">{doc.strategy_version}</Fact>
        <Fact label="Sleeve">{doc.sleeve_ref ?? 'none'}</Fact>
        <Fact label="Proposed">{etTime(doc.proposed_at)} ET</Fact>
        <Fact label="Approve by">{etTime(doc.send_deadline)} ET</Fact>
      </Facts>
    </section>
  )
}

// ---------------------------------------------------------------- pages

const PAPER_NOTE = 'Paper trading only. You approve every entry; exits run automatically.'
const FOOTER = 'Paper trading only. This page queues commands; the desk owner applies them.'

function LoadState({ poll, what }: { poll: PollState<unknown>; what: string }) {
  if (poll.data !== null) {
    return poll.error ? <p className="warn-note">The latest refresh failed ({poll.error}); showing the last list received.</p> : null
  }
  return poll.error ? (
    <p role="alert" className="load-error">
      {what} could not be loaded ({poll.error}). Nothing here is known until it loads.
    </p>
  ) : (
    <p className="muted">Loading {what.toLowerCase()}…</p>
  )
}

function ProposalIndex() {
  const poll = usePoll(getProposals)
  const ideas = usePoll(getIdeas)
  const now = useNow(1000)
  const list = poll.data
  const receivedAtMs = poll.lastSuccessAt
  const pending = list ? list.proposals.filter((r) => r.status === 'pending') : []
  pending.sort((a, b) => Date.parse(a.send_deadline) - Date.parse(b.send_deadline))
  const earlier = list ? list.proposals.filter((r) => r.status !== 'pending') : []
  const listClock = list !== null && receivedAtMs !== null ? { now: list.now, receivedAtMs } : null
  return (
    <AppShell title="Trade review" poll={poll} showRuntimeBanners={false} contextLabel="trex · paper desk" footerSource={FOOTER}>
      <div className="review-page">
        <p className="paper-note">{PAPER_NOTE}</p>
        <LoadState poll={poll} what="The proposal list" />
        {list !== null && receivedAtMs !== null && (
          <section className="card" aria-label="Waiting for your decision">
            <h2>Waiting for your decision</h2>
            {pending.length === 0 ? (
              <p className="muted">Nothing is waiting for your decision.</p>
            ) : (
              <ul className="proposal-rows">
                {pending.map((row) => {
                  const facts = approvalFromRow(row, list.now)
                  return (
                    <li key={row.proposal_id} className="proposal-row-item">
                      <a className="tap-link proposal-link" href={hashFor('trade-review-proposal', row.proposal_id)}>
                        {facts.summary}
                      </a>
                      <Countdown seconds={approvalSecondsLeft(facts, receivedAtMs, now)} />
                      <span className="muted small">
                        Max loss {formatCents(facts.max_loss_cents)}, notional {formatCents(facts.notional_cents)},{' '}
                        {row.proposal.program_id}
                      </span>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>
        )}
        <div className="initiate-grid">
          <IdeasSection poll={ideas} onComposed={poll.refresh} />
          <ManualTicketSection ideas={ideas} fallbackClock={listClock} onComposed={poll.refresh} />
        </div>
        {list !== null && receivedAtMs !== null && (
          <>
            <section className="card" aria-label="Earlier proposals">
              <h2>Earlier proposals</h2>
              {earlier.length === 0 ? (
                <p className="muted">No earlier proposals in the list.</p>
              ) : (
                <ul className="proposal-rows">
                  {earlier.map((row) => (
                    <li key={row.proposal_id} className="proposal-row-item">
                      <a className="tap-link proposal-link" href={hashFor('trade-review-proposal', row.proposal_id)}>
                        {proposalHeadline(row.proposal)}
                      </a>
                      <span className={`state-badge state-${row.status}`}>{STATUS_LABEL[row.status]}</span>
                      <span className="muted small">
                        Proposed {etTime(row.proposed_at)} ET, {row.proposal.program_id}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
            {list.problems.length > 0 && (
              <section className="card" aria-label="Unreadable proposals">
                <h2>Unreadable proposals</h2>
                <ul>
                  {list.problems.map((p) => (
                    <li key={p.file}>
                      <code>{p.file}</code>: {reasonText(p.reason)} <ReasonCode code={p.reason} />
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {list.truncated && <p className="muted small">Only the newest proposal files are listed.</p>}
          </>
        )}
      </div>
    </AppShell>
  )
}

interface SheetSnapshot {
  approval: ApprovalFacts
  receivedAtMs: number
  responseNow: Instant
}

function ProposalDetail({ proposalId }: { proposalId: string }) {
  const poll = usePoll(getProposals)
  const today = usePoll(getToday)
  const list = poll.data
  const receivedAtMs = poll.lastSuccessAt
  const row = list?.proposals.find((r) => r.proposal_id === proposalId) ?? null
  const [snapshot, setSnapshot] = useState<SheetSnapshot | null>(null)

  useEffect(() => {
    if (snapshot === null && row !== null && row.status === 'pending' && list !== null && receivedAtMs !== null) {
      setSnapshot({ approval: approvalFromRow(row, list.now), receivedAtMs, responseNow: list.now })
    }
  }, [snapshot, row, list, receivedAtMs])

  const closedReason =
    list === null ? null : row === null ? 'This proposal is no longer in the list.' : row.status === 'pending' ? null : `This proposal is now: ${STATUS_LABEL[row.status]}.`
  const now = useNow(1000)
  const sideId = useId()
  const headLeft =
    snapshot !== null && row?.status === 'pending'
      ? approvalSecondsLeft(snapshot.approval, snapshot.receivedAtMs, now)
      : null

  return (
    <AppShell title="Trade review" poll={poll} showRuntimeBanners={false} contextLabel="trex · paper desk" footerSource={FOOTER}>
      <div className="review-page">
        <a className="tap-link back" href={hashFor('trade-review')}>
          All proposals
        </a>
        <p className="paper-note">{PAPER_NOTE}</p>
        <LoadState poll={poll} what="The proposal list" />
        {list !== null && row === null && (
          <p className="warn-note">
            Proposal <code className="hash">{proposalId}</code> is not in the desk's proposal list. It may be older than
            the newest files listed, or its file could not be read
            {list.problems.length > 0 ? ` (${list.problems.length} unreadable)` : ''}.
          </p>
        )}
        {list !== null && row !== null && (
          <>
            <div className="review-head">
              <h2>{proposalHeadline(row.proposal)}</h2>
              <span className={`state-badge state-${row.status}`}>{STATUS_LABEL[row.status]}</span>
              {headLeft !== null && (
                // a visual echo of the sheet's timer, which is the one announced
                <span className="head-countdown" aria-hidden="true">
                  {headLeft > 0 ? `${formatCountdown(headLeft)} left` : 'Expired'}
                </span>
              )}
              {snapshot !== null && (
                <button
                  type="button"
                  className="secondary-button jump-to-decision"
                  onClick={() => document.getElementById(sideId)?.scrollIntoView?.({ block: 'start', behavior: 'smooth' })}
                >
                  Go to decision
                </button>
              )}
            </div>
            <div className="review-grid">
              <ProposalFacts row={row} list={list} today={today} />
              <div className="review-side" id={sideId}>
                {snapshot !== null ? (
                  <ApprovalSheet
                    approval={snapshot.approval}
                    receivedAtMs={snapshot.receivedAtMs}
                    responseNow={snapshot.responseNow}
                    mode="inline"
                    closedReason={closedReason}
                    onSettled={() => {
                      poll.refresh()
                      today.refresh()
                    }}
                  />
                ) : (
                  <p className="muted">
                    {row.status === 'pending' ? 'Preparing the decision…' : `Nothing to decide: ${STATUS_LABEL[row.status].toLowerCase()}.`}
                  </p>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </AppShell>
  )
}

/** `#/trade-review` (no id) or `#/trade-review/:proposalId`. */
export function TradeReviewPage({ proposalId = null }: { proposalId?: string | null }) {
  return proposalId ? <ProposalDetail key={proposalId} proposalId={proposalId} /> : <ProposalIndex />
}
