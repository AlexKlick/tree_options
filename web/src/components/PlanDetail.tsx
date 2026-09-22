import { getPlan } from '../lib/api'
import { usePoll } from '../hooks/usePoll'
import { AppShell } from './AppShell'

// Placeholder until C3 lands the full detail view (tiles, marks, ledger);
// the live Jinja panel still serves /plan/<id> until the cutover commit.
export function PlanDetail({ id }: { id: string }) {
  const poll = usePoll(() => getPlan(id))
  const plan = poll.data?.plan
  return (
    <AppShell title={plan ? plan.id : id} poll={poll}>
      <div className="card">
        <p className="muted">
          {poll.data
            ? `${poll.data.plan.account_mode.toUpperCase()} · ${poll.data.plan.structures.length} structures — detail view arriving with the next commit.`
            : poll.error
              ? `Cannot reach the cockpit API. ${poll.error}`
              : 'Loading…'}
        </p>
      </div>
    </AppShell>
  )
}
