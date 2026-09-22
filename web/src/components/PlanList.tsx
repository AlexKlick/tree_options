import { getPlans } from '../lib/api'
import { navigate } from '../lib/router'
import { usd, usdSigned } from '../lib/format'
import { usePoll } from '../hooks/usePoll'
import type { PlanSummary } from '../lib/types'
import { AppShell } from './AppShell'
import { Pill } from './Pill'
import { PortfolioTiles } from './PortfolioTiles'
import { NetPositionsTable } from './NetPositionsTable'

function WindowPill({ p }: { p: PlanSummary }) {
  if (p.window_state === 'during') return <Pill variant="armed">● window open</Pill>
  if (p.window_state === 'before')
    return <Pill variant="empty">○ window {p.entry_window_start} ET</Pill>
  if (p.window_state === 'after')
    return <Pill variant="disarmed">● window closed</Pill>
  return <Pill variant="empty">○ entry {p.entry_date}</Pill>
}

function PlanCard({ p }: { p: PlanSummary }) {
  const pnlClass = (v: number | null) =>
    v === null ? '' : v >= 0 ? 'pnl-pos' : 'pnl-neg'
  return (
    <button
      type="button"
      className="card plan-card"
      onClick={() => navigate({ view: 'plan', id: p.id })}
    >
      <div className="card-head">
        <h3>{p.id}</h3>
        {p.worst_state ? (
          <span className={`badge badge-${p.worst_state}`}>{p.worst_state}</span>
        ) : (
          <Pill variant="empty">no state</Pill>
        )}
      </div>
      <p className="muted">
        {p.account_mode.toUpperCase()} · {p.structure_count} structures · cap{' '}
        {usd(p.total_debit_cap)}
      </p>
      <p className="num">
        {p.unrealized_open !== null ? (
          <span className={pnlClass(p.unrealized_open)}>
            open {usdSigned(p.unrealized_open)}
          </span>
        ) : (
          <span className="muted">open —</span>
        )}
        {' · '}
        {p.realized !== null ? (
          <span className={pnlClass(p.realized)}>realized {usdSigned(p.realized)}</span>
        ) : (
          <span className="muted">realized —</span>
        )}
      </p>
      <div className="pill-row">
        {p.armed ? (
          <Pill variant="armed">● armed</Pill>
        ) : p.heartbeat ? (
          <Pill variant="disarmed">● disarmed</Pill>
        ) : (
          <Pill variant="empty">○ no heartbeat</Pill>
        )}
        <WindowPill p={p} />
        <span className="muted card-open-hint">open plan →</span>
      </div>
    </button>
  )
}

export function PlanList() {
  const poll = usePoll(getPlans)
  const d = poll.data
  return (
    <AppShell title="Plans" poll={poll}>
      {poll.data === null && poll.error ? (
        <div className="card empty-state">
          <p className="muted">
            Cannot reach the cockpit API. {poll.error}
          </p>
        </div>
      ) : poll.data && poll.data.plans.length === 0 ? (
        <div className="card empty-state">
          <div className="icon">∅</div>
          <h2>No plans found</h2>
          <p className="muted">No plan TOMLs in the plans directory.</p>
        </div>
      ) : (
        <>
          <PortfolioTiles portfolio={d?.portfolio ?? null} account={d?.account ?? null} />
          {(d?.net_positions ?? []).length > 0 ? (
            <>
              <h2 className="section-title" style={{ marginTop: 24 }}>
                Current positions{' '}
                <span className="muted section-sub">
                  (all plans · per underlying · click a plan for legs, fills, payoff)
                </span>
              </h2>
              <NetPositionsTable rows={d?.net_positions ?? []} />
            </>
          ) : null}
          <h2 className="section-title" style={{ marginTop: 24 }}>Plans</h2>
          <div className="grid">
            {d?.plans.map((p) => <PlanCard key={p.id} p={p} />)}
          </div>
        </>
      )}
    </AppShell>
  )
}
