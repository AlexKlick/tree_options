// Ideas tab of the symbol page (Phase 5a viewer upgrade). THE PROTOCOL
// BOUNDARY IS THE POINT: the desk is advisory, only allowed_direction
// signals (xsmom_top3, pead_beat) may point a trade, the queue is the
// miner's PROPOSED selection pending an operator ruling, positions are
// paper, and the card-history / research sections are explicitly
// non-directional context. Every one of those copy strings is first-class
// and pinned by tests. Null sections render their honest empty states.

import { getSymbolIdeas } from '../../lib/api'
import { usePoll } from '../../hooks/usePoll'
import { ago, num2 } from '../../lib/format'
import type {
  IdeasCardHistory,
  IdeasDeal,
  IdeasPaperPosition,
  IdeasProtocol,
  IdeasQueue,
  IdeasResearch,
  IdeasSignals,
  SymbolIdeas,
} from '../../lib/types'
import { Pill } from '../Pill'
import { TableScroll } from '../TableScroll'

const signedPct = (v: number): string => `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`

const pct = (v: number): string => `${(v * 100).toFixed(1)}%`

const nz = (v: number | null | undefined, fmt: (n: number) => string): string =>
  v === null || v === undefined ? '—' : fmt(v)

const strikeFmt = (v: number): string =>
  v % 1 === 0 ? v.toFixed(0) : v.toFixed(1)

const ageNote = (seconds: number | null | undefined): string =>
  seconds === null || seconds === undefined ? '' : ` · data ${ago(seconds)} old`

export function IdeasPanel({ sym }: { sym: string }) {
  const poll = usePoll(() => getSymbolIdeas(sym), 60_000)
  const d: SymbolIdeas | null = poll.data

  if (d === null) {
    return (
      <div className="card empty-state">
        <p className="muted">
          {poll.error
            ? `Ideas surface not reachable yet. ${poll.error}`
            : 'Loading ideas…'}
        </p>
      </div>
    )
  }

  return (
    <>
      {d.protocol.advisory && (
        <div className="pill-row" style={{ marginBottom: 12 }}>
          <Pill variant="empty">
            advisory — the desk suggests, the operator decides
          </Pill>
        </div>
      )}

      <SignalsCard sym={sym} s={d.signals} protocol={d.protocol} />
      <QueueSection queue={d.queue} />
      <PaperSection rows={d.paper_positions} />
      <CardsSection cards={d.cards} />
      <ResearchSection research={d.research} />

      <p className="table-note muted" style={{ marginTop: 18 }}>
        survivors are actionable-as-information only; nothing here points a
        trade — only ALLOWED_DIRECTION signals may.
      </p>
      <p className="table-note muted">
        protocol: allowed_direction{' '}
        {d.protocol.allowed_direction.join(', ') || 'none'} · context_only{' '}
        {d.protocol.context_only.join(', ') || 'none'}
      </p>
    </>
  )
}

// -------------------------------------------------------------- signals

function SignalsCard({
  sym,
  s,
  protocol,
}: {
  sym: string
  s: IdeasSignals | null
  protocol: IdeasProtocol
}) {
  if (s === null) {
    return (
      <>
        <h2 className="section-title">Signals</h2>
        <div className="card empty-state">
          <p className="muted">no signals yet for {sym}</p>
        </div>
      </>
    )
  }

  // Direction chips are DERIVED from protocol.allowed_direction — the
  // panel never hardcodes which signals may point a trade.
  const chips: string[] = []
  if (s.xsmom.in_top3 && protocol.allowed_direction.includes('xsmom_top3')) {
    chips.push('xsmom_top3 · ALLOWED_DIRECTION')
  }
  if (s.pead.beats.length > 0 && protocol.allowed_direction.includes('pead_beat')) {
    chips.push('pead_beat · ALLOWED_DIRECTION')
  }

  const latestEvaluated =
    s.pead.evaluated.length > 0
      ? s.pead.evaluated[s.pead.evaluated.length - 1]
      : null

  return (
    <>
      <h2 className="section-title">
        Signals{' '}
        <span className="muted section-sub">
          session {s.session}
          {ageNote(s.age_seconds)}
        </span>
      </h2>
      {chips.length > 0 && (
        <div className="pill-row" style={{ marginBottom: 10 }}>
          {chips.map((c) => (
            <Pill key={c} variant="armed">
              {c}
            </Pill>
          ))}
        </div>
      )}
      <div className="card">
        <div className="card-head">
          <div>
            <h3>xsmom</h3>
            <p className="muted section-sub">
              ranked over {s.xsmom.n_ranked} names ·{' '}
              {s.xsmom.is_rebalance_day
                ? 'rebalance day — ranks recompute today'
                : 'not a rebalance day — ranks carry'}
            </p>
          </div>
          <div className="num" style={{ fontSize: '1.5rem', fontWeight: 700 }}>
            {s.xsmom.score !== null ? s.xsmom.score.toFixed(2) : '—'}
          </div>
        </div>
        <div className="pill-row" style={{ margin: '10px 0' }}>
          {s.xsmom.top3.map((t) =>
            t === sym ? (
              <span key={t} className="badge badge-open">
                {t}
              </span>
            ) : (
              <span key={t} className="badge badge-planned">
                {t}
              </span>
            ),
          )}
        </div>
        {!s.xsmom.conventions_agree && (
          <Pill variant="disarmed">xsmom conventions disagree</Pill>
        )}
      </div>

      <div className="card">
        <h3>PEAD events</h3>
        {s.pead.beats.length === 0 && !latestEvaluated ? (
          <p className="muted">no PEAD events recorded for {sym}</p>
        ) : (
          <ul className="news-list">
            {s.pead.beats.map((b) => (
              <li key={`b:${b.report_date}`}>
                beat {b.report_date} · {signedPct(b.move)}
              </li>
            ))}
            {latestEvaluated && (
              <li>
                evaluated {latestEvaluated.report_date}
                {latestEvaluated.prior_session
                  ? ` (prior ${latestEvaluated.prior_session})`
                  : ''}
                {' · move '}
                {nz(latestEvaluated.move, signedPct)}
                {' · '}
                {latestEvaluated.fires ? 'fires' : 'does not fire'}
                {latestEvaluated.reason ? ` — ${latestEvaluated.reason}` : ''}
              </li>
            )}
          </ul>
        )}
        <p className="table-note muted">next report {s.next_report ?? '—'}</p>
      </div>
    </>
  )
}

// ---------------------------------------------------------------- queue

function QueueSection({ queue }: { queue: IdeasQueue | null }) {
  if (queue === null) {
    return (
      <>
        <h2 className="section-title">Queue deals</h2>
        <div className="card empty-state">
          <p className="muted">
            no queue yet — the miner's selection rule is pending an operator
            ruling
          </p>
        </div>
      </>
    )
  }
  return (
    <>
      <h2 className="section-title">
        Queue deals{' '}
        <span className="muted section-sub">
          session {queue.session} · entry {queue.entry_session} · valid until{' '}
          {queue.valid_until}
        </span>
      </h2>
      <div className="pill-row" style={{ marginBottom: 10 }}>
        <Pill variant="disarmed">
          {queue.miner_status === 'PROPOSED'
            ? `selection rule ${queue.miner_status} — pending operator ruling`
            : `selection rule ${queue.miner_status}`}
        </Pill>
      </div>
      {queue.deals.length === 0 ? (
        <div className="card empty-state">
          <p className="muted">the queue for {queue.session} is empty</p>
        </div>
      ) : (
        queue.deals.map((deal) => <DealCard key={deal.deal_id} deal={deal} />)
      )}
    </>
  )
}

function DealCard({ deal }: { deal: IdeasDeal }) {
  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h3>
            #{deal.rank} {deal.row_title}
          </h3>
          <p className="muted section-sub">
            deal {deal.deal_id} · {deal.kind} · {deal.underlying} · qty{' '}
            {deal.quantity}
          </p>
        </div>
        <div className="pill-col">
          <span className="badge badge-planned">{deal.status}</span>
          {deal.signal ? (
            <Pill variant="empty">
              signal {deal.signal.name}
              {deal.signal.excess_20 !== null && deal.signal.excess_20 !== undefined
                ? ` · excess20 ${deal.signal.excess_20.toFixed(2)}`
                : ''}
            </Pill>
          ) : (
            <Pill variant="empty">no pointing signal — context only</Pill>
          )}
        </div>
      </div>
      <TableScroll>
        <table>
          <thead>
            <tr>
              <th>Right</th>
              <th>Action</th>
              <th className="num">Strike</th>
              <th>Expiry</th>
              <th className="num">Bid</th>
              <th className="num">Ask</th>
              <th className="num">OI</th>
              <th className="num">IV</th>
              <th className="num">Delta</th>
            </tr>
          </thead>
          <tbody>
            {deal.legs.map((leg, i) => (
              <tr key={i}>
                <td>{leg.right}</td>
                <td>{leg.action}</td>
                <td className="num strike">{strikeFmt(leg.strike)}</td>
                <td className="nowrap">{leg.expiry}</td>
                <td className="num">{nz(leg.bid, num2)}</td>
                <td className="num">{nz(leg.ask, num2)}</td>
                <td className="num">{nz(leg.oi, (n) => n.toLocaleString('en-US'))}</td>
                <td className="num">{nz(leg.iv, pct)}</td>
                <td className="num">{nz(leg.delta, num2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
      <dl className="kv">
        <dt>ref mid</dt>
        <dd>{nz(deal.ref_mid, num2)}</dd>
        <dt>fill</dt>
        <dd>{nz(deal.fill, num2)}</dd>
        <dt>limit</dt>
        <dd>{nz(deal.limit, num2)}</dd>
        <dt>max loss</dt>
        <dd>{nz(deal.max_loss, num2)}</dd>
      </dl>
      {deal.reasons.length > 0 && (
        <ul className="rule-list">
          {deal.reasons.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      )}
      {deal.notes.length > 0 && (
        <ul className="rule-list muted">
          {deal.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ---------------------------------------------------------- paper positions

function PaperSection({ rows }: { rows: IdeasPaperPosition[] }) {
  return (
    <>
      <h2 className="section-title">Paper positions</h2>
      <div className="pill-row" style={{ marginBottom: 10 }}>
        <Pill variant="empty">paper · not executed</Pill>
      </div>
      {rows.length === 0 ? (
        <div className="card empty-state">
          <p className="muted">no paper positions</p>
        </div>
      ) : (
        <div className="card table-card">
          <TableScroll>
            <table>
              <thead>
                <tr>
                  <th>Plan</th>
                  <th>Structure</th>
                  <th>Mode</th>
                  <th>Expiry</th>
                  <th className="num">Long K</th>
                  <th className="num">Short K</th>
                  <th className="num">Qty</th>
                  <th className="num">Open</th>
                  <th>Status</th>
                  <th className="num">Entry fill</th>
                  <th>Exit deadline</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.structure_id}>
                    <td>
                      <a href={`#/plan/${p.plan_id}`}>{p.plan_id}</a>
                    </td>
                    <td className="nowrap">{p.structure_id}</td>
                    <td>{p.account_mode}</td>
                    <td className="nowrap">{p.expiry}</td>
                    <td className="num strike">{strikeFmt(p.long_strike)}</td>
                    <td className="num strike">{strikeFmt(p.short_strike)}</td>
                    <td className="num">{p.quantity}</td>
                    <td className="num">{p.open_qty}</td>
                    <td>
                      <span className="badge badge-planned">{p.status}</span>
                    </td>
                    <td className="num">{nz(p.entry_fill, num2)}</td>
                    <td className="nowrap">{p.exit_deadline}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        </div>
      )}
    </>
  )
}

// ------------------------------------------------------------ card history

function CardsSection({ cards }: { cards: IdeasCardHistory | null }) {
  return (
    <>
      <h2 className="section-title">Card history</h2>
      <div className="pill-row" style={{ marginBottom: 10 }}>
        <Pill variant="disarmed">sealed scratch lane · not the desk protocol</Pill>
      </div>
      {cards === null ? (
        <div className="card empty-state">
          <p className="muted">no card history yet</p>
        </div>
      ) : (
        <div className="card">
          <p className="table-note muted" style={{ margin: '0 0 10px' }}>
            scratch ledger {cards.ledger_sha256_12}
          </p>
          {/* plain text, exactly as sealed — no markdown rendering */}
          <pre>{cards.lines.join('\n')}</pre>
        </div>
      )}
    </>
  )
}

// --------------------------------------------------------- research context

function ResearchSection({ research }: { research: IdeasResearch | null }) {
  return (
    <>
      <h2 className="section-title">Research context</h2>
      <div className="pill-row" style={{ marginBottom: 10 }}>
        <Pill variant="empty">research state · non-directional</Pill>
      </div>
      {research === null ? (
        <div className="card empty-state">
          <p className="muted">no research ledger context</p>
        </div>
      ) : (
        <div className="card">
          <p className="table-note muted" style={{ margin: '0 0 10px' }}>
            ledger {research.ledger_date} · {research.sha256_12}
          </p>
          {[...new Set(research.entries.map((e) => e.section))].map((section) => (
            <div key={section} style={{ marginBottom: 12 }}>
              <h3>{section}</h3>
              <ul className="rule-list">
                {research.entries
                  .filter((e) => e.section === section)
                  .map((e, i) => (
                    <li key={i}>{e.line}</li>
                  ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
