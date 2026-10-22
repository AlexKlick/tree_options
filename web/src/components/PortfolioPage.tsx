// Portfolio (#/portfolio): the desk owner's books as the files stand, read
// from GET /api/portfolio (trex-portfolio/1). Read-only. Every block that the
// server marks unavailable shows its reason; no amount, count or age is ever
// invented for it.

import type { ReactNode } from 'react'
import { usePoll } from '../hooks/usePoll'
import { reasonText } from '../lib/activityApi'
import { CONTROL_LABELS, type Holding, type PortfolioView } from '../lib/contracts'
import { ago, etDateTime } from '../lib/format'
import {
  MISMATCH_LABELS,
  SEC_TYPE_LABELS,
  centsText,
  getPortfolio,
  holdingSplit,
  microsText,
  symbolFor,
} from '../lib/portfolioApi'
import { AppShell } from './AppShell'
import './accountPages.css'

export type Tone = 'ok' | 'warn' | 'bad' | 'unknown'

export function StatusPill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`acct-status acct-status-${tone}`}>{children}</span>
}

/** An unknown fact: the words, the machine code, never a stand-in value. */
export function Unavailable({ reason, lead = 'Unavailable' }: { reason: string; lead?: string }) {
  return (
    <p className="acct-unavailable">
      {lead}: {reasonText(reason)}
    </p>
  )
}

export function Section({
  title,
  id,
  status,
  children,
}: {
  title: string
  id: string
  status?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="card" aria-labelledby={id}>
      <div className="acct-head">
        <h2 id={id}>{title}</h2>
        {status}
      </div>
      {children}
    </section>
  )
}

const asOfText = (asOf: string, ageS: number) => `${etDateTime(asOf)} (${ago(ageS)} old)`

function snapshotTone(status: 'fresh' | 'stale' | 'unavailable'): [Tone, string] {
  if (status === 'fresh') return ['ok', 'Fresh']
  if (status === 'stale') return ['warn', 'Stale']
  return ['unknown', 'Unavailable']
}

function AccountSection({ view }: { view: PortfolioView }) {
  return (
    <Section title="Account" id="pf-account">
      {view.account_ids.length === 0 ? (
        <p>No account recorded yet: the books, the risk snapshot and the reconciliation are all unavailable.</p>
      ) : (
        <p>
          Paper account{view.account_ids.length === 1 ? '' : 's'}: <strong>{view.account_ids.join(', ')}</strong>
        </p>
      )}
      <p className="muted">Paper only: TREX never sends a live order, and this page sends nothing.</p>
      {view.problems.map((problem, index) => (
        <p role="alert" className="acct-alert" key={`${problem.reason}-${index}`}>
          {problem.reason === 'non_paper_account'
            ? `Account ${problem.account_id ?? '(unnamed)'} is not a paper account. TREX trades paper accounts only; stop entries and tell the host.`
            : `${reasonText(problem.reason)}${problem.detail ? `: ${problem.detail}` : ''}`}
        </p>
      ))}
    </Section>
  )
}

function EntriesSection({ view }: { view: PortfolioView }) {
  return (
    <Section title="What blocks new entries" id="pf-entries">
      {view.entry_blockers.length === 0 ? (
        <p>Nothing in the books blocks new entries.</p>
      ) : (
        <ul>
          {view.entry_blockers.map((code) => (
            <li key={code}>{reasonText(code)}</li>
          ))}
        </ul>
      )}
      {view.kill_files.PAUSE_ENTRIES && (
        <p>{CONTROL_LABELS.pause_entries}: standing. Pausing never stops exits.</p>
      )}
      {view.kill_files.FLATTEN && <p>{CONTROL_LABELS.flatten}: standing. The desk is closing its positions.</p>}
      <p>
        <strong>
          {view.exits_blocked
            ? `Exits are stopped: ${CONTROL_LABELS.halt} is standing, so open positions are not being watched.`
            : 'Exits run automatically, also while new entries are paused, as long as the desk owner is running.'}
        </strong>
      </p>
      {view.exits_blocked ? null : (
        <p className="muted">
          This page does not see the desk owner: whether it is running exits right now is on{' '}
          <a href="#/today">Today</a> (Desk owner).
        </p>
      )}
      <p className="muted">
        The controls live in the control bar at the top of every page. <a href="#/today">Open Today</a> for the
        current state.
      </p>
    </Section>
  )
}

function CashSection({ view }: { view: PortfolioView }) {
  const { cash, holdings } = view
  const [tone, label] = snapshotTone(cash.status)
  return (
    <Section title="Cash" id="pf-cash" status={<StatusPill tone={tone}>{label}</StatusPill>}>
      <dl className="kv">
        <dt>Settled cash (broker)</dt>
        <dd>
          {cash.status === 'unavailable' ? (
            <Unavailable reason={cash.reason} />
          ) : (
            <span className="num">{centsText(cash.settled_cash_cents)}</span>
          )}
        </dd>
        <dt>As of</dt>
        <dd>{cash.as_of !== null && cash.age_s !== null ? asOfText(cash.as_of, cash.age_s) : 'Not recorded'}</dd>
        <dt>Cash in TREX's books</dt>
        <dd>
          {holdings.status === 'unavailable' ? (
            <Unavailable reason={holdings.reason} />
          ) : (
            <span className="num">{microsText(holdings.ledger_cash_micros)}</span>
          )}
        </dd>
      </dl>
      {cash.status === 'stale' && <p className="muted">Stale: {reasonText(cash.reason ?? 'risk_snapshot_stale')}</p>}
    </Section>
  )
}

function RiskSection({ view }: { view: PortfolioView }) {
  const { risk } = view
  const [tone, label] = snapshotTone(risk.status)
  return (
    <Section title="Risk snapshot" id="pf-risk" status={<StatusPill tone={tone}>{label}</StatusPill>}>
      {risk.status === 'unavailable' ? (
        <>
          <Unavailable reason={risk.reason} />
          {risk.as_of !== null && risk.age_s !== null && (
            <p className="muted">Last snapshot written {asOfText(risk.as_of, risk.age_s)}; its figures are not trusted.</p>
          )}
        </>
      ) : (
        <>
          <p>
            Snapshot {ago(risk.age_s)} old; it counts as stale after {ago(risk.stale_after_s)}.
          </p>
          {risk.status === 'stale' && (
            <p className="muted">New entries stay blocked until a fresh snapshot arrives. Exits do not wait for it.</p>
          )}
          <dl className="kv">
            <dt>Open loss reserved</dt>
            <dd className="num">{centsText(risk.open_loss_cents)}</dd>
            <dt>Realized loss today</dt>
            <dd className="num">{centsText(risk.day_realized_loss_cents)}</dd>
            <dt>Stock held (gross)</dt>
            <dd className="num">{centsText(risk.equity_gross_cents)}</dd>
            <dt>Taken</dt>
            <dd>{etDateTime(risk.as_of)}</dd>
          </dl>
        </>
      )}
    </Section>
  )
}

function lotText(lot: Holding['lots'][number]): string {
  const a = lot.attribution
  if (a.intent_id === null) return `Outside TREX: ${lot.quantity}`
  return `${a.program_id} · intent ${a.intent_id} · ${a.strategy_version} · sleeve ${a.sleeve_ref}: ${lot.quantity}`
}

function HoldingRow({ holding }: { holding: Holding }) {
  const split = holdingSplit(holding)
  return (
    <li className="acct-row">
      <div className="acct-row-head">
        <strong>{holding.symbol}</strong>
        <span>{SEC_TYPE_LABELS[holding.sec_type]}</span>
        <span className="num">
          {holding.quantity > 0 ? 'Long' : 'Short'} {Math.abs(holding.quantity)}
        </span>
      </div>
      <p className="acct-row-meta">
        Cost basis {microsText(holding.cost_basis_micros)} · contract {holding.con_id}
      </p>
      <p className="acct-row-meta">
        {split.outside === 0 ? 'All TREX' : `${split.trex} TREX, ${split.outside} outside TREX`}
      </p>
      <ul className="acct-row-meta">
        {holding.lots.map((lot) => (
          <li key={`${lot.attribution.intent_id ?? 'outside'}-${lot.attribution.sleeve_ref}`}>{lotText(lot)}</li>
        ))}
      </ul>
    </li>
  )
}

function HoldingsSection({ view }: { view: PortfolioView }) {
  const { holdings } = view
  return (
    <Section
      title="Holdings"
      id="pf-holdings"
      status={
        holdings.status === 'unavailable' ? (
          <StatusPill tone="unknown">Unavailable</StatusPill>
        ) : (
          <StatusPill tone="ok">From the books</StatusPill>
        )
      }
    >
      {holdings.status === 'unavailable' ? (
        <Unavailable reason={holdings.reason} />
      ) : (
        <>
          <p className="muted">As of {asOfText(holdings.as_of, holdings.age_s)}.</p>
          {holdings.rows.length === 0 ? (
            <p>TREX's books hold no positions.</p>
          ) : (
            <ul className="acct-list">
              {holdings.rows.map((holding) => (
                <HoldingRow key={holding.con_id} holding={holding} />
              ))}
            </ul>
          )}
        </>
      )}
    </Section>
  )
}

function OutsideSection({ view }: { view: PortfolioView }) {
  const { outside_positions: outside } = view
  const [tone, label] = snapshotTone(outside.status)
  const outsideLots = (view.holdings.rows ?? []).filter((row) => holdingSplit(row).outside !== 0)
  return (
    <Section title="Positions outside TREX" id="pf-outside" status={<StatusPill tone={tone}>{label}</StatusPill>}>
      <p className="muted">
        Broker positions in contracts TREX's books never touched. TREX never trades or closes these; they still count
        in the account's risk.
      </p>
      {outside.status === 'unavailable' ? (
        <Unavailable reason={outside.reason} />
      ) : outside.rows.length === 0 ? (
        <p>None at the broker.</p>
      ) : (
        <ul className="acct-list">
          {outside.rows.map((row) => (
            <li className="acct-row" key={row.con_id}>
              <div className="acct-row-head">
                <strong>{row.symbol}</strong>
                <span>{SEC_TYPE_LABELS[row.sec_type]}</span>
                <span className="num">
                  {row.quantity > 0 ? 'Long' : 'Short'} {Math.abs(row.quantity)}
                </span>
              </div>
              <p className="acct-row-meta">contract {row.con_id}</p>
            </li>
          ))}
        </ul>
      )}
      {outsideLots.length > 0 && (
        <p className="muted">
          Also outside TREX inside held contracts:{' '}
          {outsideLots.map((row) => `${row.symbol} ${SEC_TYPE_LABELS[row.sec_type]} ${holdingSplit(row).outside}`).join('; ')}
          .
        </p>
      )}
    </Section>
  )
}

function ReconciliationSection({ view }: { view: PortfolioView }) {
  const rec = view.reconciliation
  const checked = rec.as_of !== null && rec.age_s !== null ? asOfText(rec.as_of, rec.age_s) : null
  const [tone, label]: [Tone, string] =
    rec.status === 'reconciliation_required'
      ? ['bad', 'Mismatch']
      : rec.status === 'clean'
        ? rec.fresh
          ? ['ok', 'Clean']
          : ['warn', 'Clean but old']
        : ['unknown', 'Unavailable']
  return (
    <Section title="Reconciliation" id="pf-reconciliation" status={<StatusPill tone={tone}>{label}</StatusPill>}>
      {rec.status === 'unavailable' && (
        <>
          <Unavailable reason={rec.reason ?? 'reconciliation_unavailable'} />
          {checked && <p className="muted">Last record {checked}.</p>}
        </>
      )}
      {rec.status === 'clean' && (
        <p>
          {rec.fresh
            ? `TREX's books match the broker, checked ${checked}.`
            : `The last check matched, but it is old: checked ${checked}. ${reasonText(rec.reason ?? 'reconciliation_stale')}`}
        </p>
      )}
      {rec.status === 'reconciliation_required' && (
        <>
          <p>
            TREX's books and the broker do not match, checked {checked}. New entries stay blocked until they match
            again; exits continue.
          </p>
          {/* data-label: on a phone each row stacks as label/value pairs */}
          <table className="acct-mismatch" aria-label="Mismatched contracts">
            <thead>
              <tr>
                <th scope="col">Contract</th>
                <th scope="col">Id</th>
                <th scope="col" className="num">TREX books</th>
                <th scope="col" className="num">Broker</th>
                <th scope="col">What differs</th>
              </tr>
            </thead>
            <tbody>
              {(rec.mismatches ?? []).map((m) => (
                <tr key={m.con_id}>
                  <td data-label="Contract">{symbolFor(view, m.con_id) ?? 'Unknown contract'}</td>
                  <td data-label="Id">{m.con_id}</td>
                  <td data-label="TREX books" className="num">{m.local_qty}</td>
                  <td data-label="Broker" className="num">{m.broker_qty}</td>
                  <td data-label="What differs">{MISMATCH_LABELS[m.kind] ?? m.kind}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Section>
  )
}

/** The portfolio sections for one known document (no fetching). */
export function PortfolioSections({ view }: { view: PortfolioView }) {
  return (
    <>
      <AccountSection view={view} />
      <div className="acct-grid">
        <CashSection view={view} />
        <RiskSection view={view} />
      </div>
      <ReconciliationSection view={view} />
      <HoldingsSection view={view} />
      <OutsideSection view={view} />
      <EntriesSection view={view} />
    </>
  )
}

export function PortfolioPage() {
  const poll = usePoll(getPortfolio)
  const view = poll.data
  return (
    <AppShell
      title="Portfolio"
      poll={poll}
      showRuntimeBanners={false}
      contextLabel="trex · paper portfolio"
      footerSource="Read-only: the desk owner's books, risk snapshot and reconciliation. Nothing here sends an order."
    >
      <div className="acct-page">
        <p className="acct-intro">What TREX holds, the cash beside it, and whether the books match the broker.</p>
        {poll.error && (
          <p role="alert" className="acct-alert">
            The portfolio could not be read: {poll.error}.{view ? ' Showing the last portfolio received.' : ''}
          </p>
        )}
        {!view && !poll.error && <p role="status">Loading the portfolio…</p>}
        {view && <PortfolioSections view={view} />}
      </div>
    </AppShell>
  )
}
