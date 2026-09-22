import { getPlans } from '../lib/api'
import { navigate } from '../lib/router'
import { usd } from '../lib/format'
import { usePoll } from '../hooks/usePoll'
import type { PlanSummary } from '../lib/types'
import { AppShell } from './AppShell'
import { Pill } from './Pill'

function WindowPill({ p }: { p: PlanSummary }) {
  if (p.window_state === 'during') return <Pill variant="armed">● window open</Pill>
  if (p.window_state === 'before')
    return <Pill variant="empty">○ window {p.entry_window_start} ET</Pill>
  if (p.window_state === 'after')
    return <Pill variant="disarmed">● window closed</Pill>
  return <Pill variant="empty">○ entry {p.entry_date}</Pill>
}

function PlanCard({ p }: { p: PlanSummary }) {
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
      <div className="pill-row">
        {p.armed ? (
          <Pill variant="armed">● armed</Pill>
        ) : p.heartbeat ? (
          <Pill variant="disarmed">● disarmed</Pill>
        ) : (
          <Pill variant="empty">○ no heartbeat</Pill>
        )}
        <WindowPill p={p} />
      </div>
    </button>
  )
}

export function PlanList() {
  const poll = usePoll(getPlans)
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
        <div className="grid">
          {poll.data?.plans.map((p) => <PlanCard key={p.id} p={p} />)}
        </div>
      )}
    </AppShell>
  )
}
