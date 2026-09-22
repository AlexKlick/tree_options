import { getPlan } from '../lib/api'
import type { Payoff, PlanDetailResponse } from '../lib/types'
import { usePoll } from '../hooks/usePoll'
import { useDensity } from '../density'
import { AppShell } from './AppShell'
import { EventsLedger } from './EventsLedger'
import { MarksTable } from './MarksTable'
import { PayoffCard } from './PayoffCard'
import { PnlHistoryChart } from './PnlHistoryChart'
import { PositionsTable } from './PositionsTable'
import { RunbookBanner } from './RunbookBanner'
import { StatTiles } from './StatTiles'
import { Tabs } from './Tabs'

function ChartPlaceholder({ note }: { note: string }) {
  return (
    <div className="card">
      <p className="muted" style={{ margin: 0 }}>
        {note}
      </p>
    </div>
  )
}

function HistorySection({ history }: { history: PlanDetailResponse['history'] }) {
  return history ? (
    <div className="card chart-card">
      <PnlHistoryChart series={history} />
    </div>
  ) : (
    <ChartPlaceholder note="P&L history appears after the monitor records a few ticks." />
  )
}

function PayoffSection({ payoffs }: { payoffs: Payoff[] }) {
  return payoffs.length > 0 ? (
    <div className="grid">
      {payoffs.map((p) => (
        <PayoffCard key={p.structure_id} payoff={p} />
      ))}
    </div>
  ) : (
    <ChartPlaceholder note="Payoff curves appear once a structure fills." />
  )
}

export function PlanDetail({ id }: { id: string }) {
  const poll = usePoll(() => getPlan(id))
  const { mode } = useDensity()
  const d = poll.data

  const footer = d ? (
    <span>
      raw{' '}
      <a href={`plan/${encodeURIComponent(id)}/book.json`}>book.json</a> ·{' '}
      <a href={`plan/${encodeURIComponent(id)}/events.jsonl`}>events.jsonl</a>
    </span>
  ) : undefined

  return (
    <AppShell title={d ? d.plan.id : id} poll={poll} footerExtra={footer}>
      {d === null && poll.error ? (
        <div className="card empty-state">
          <div className="icon">⚠</div>
          <h2>{poll.error.includes('404') ? 'Plan not found' : 'Cannot reach the cockpit API'}</h2>
          <p className="muted">{poll.error}</p>
          <p>
            <a href="#/">← All plans</a>
          </p>
        </div>
      ) : d === null ? (
        <div className="card empty-state">
          <p className="muted">Loading…</p>
        </div>
      ) : mode === 'simple' ? (
        <>
          <RunbookBanner d={d} compact />
          <StatTiles summary={d.book_summary} marks={d.marks} />
          <Tabs
            tabs={[
              {
                id: 'performance',
                label: 'Performance',
                content: (
                  <>
                    <MarksTable marks={d.marks} specs={d.plan.structures} />
                    <HistorySection history={d.history} />
                  </>
                ),
              },
              {
                id: 'payoff',
                label: 'Payoff',
                content: <PayoffSection payoffs={d.payoffs} />,
              },
              {
                id: 'positions',
                label: 'Positions',
                content: (
                  <PositionsTable specs={d.plan.structures} structures={d.structures} />
                ),
              },
              {
                id: 'ledger',
                label: 'Ledger',
                content: <EventsLedger events={d.events} />,
              },
            ]}
          />
        </>
      ) : (
        <>
          <a className="back" href="#/">
            ← All plans
          </a>
          <RunbookBanner d={d} />
          <StatTiles summary={d.book_summary} marks={d.marks} />
          <h2 className="section-title">
            Live marks{' '}
            <span className="muted section-sub">
              (delayed quotes · mark-to-mid · refresh ~20s)
            </span>
          </h2>
          <MarksTable marks={d.marks} specs={d.plan.structures} />
          <h2 className="section-title">
            Unrealized since entry{' '}
            <span className="muted section-sub">
              (total book · every monitor tick · time-proportional)
            </span>
          </h2>
          <HistorySection history={d.history} />
          <h2 className="section-title">
            Payoff at expiry{' '}
            <span className="muted section-sub">
              (terminal value · the discipline exits long before)
            </span>
          </h2>
          <PayoffSection payoffs={d.payoffs} />
          <h2 className="section-title">Positions</h2>
          <PositionsTable specs={d.plan.structures} structures={d.structures} />
          <h2 className="section-title">
            Events <span className="muted section-sub">(last {d.events.length})</span>
          </h2>
          <EventsLedger events={d.events} />
        </>
      )}
    </AppShell>
  )
}
