// Exit-machine (trex-monitor) health banner, on every page (AppShell). Fed
// by the trex-exit-watch timer via GET /api/exit-machine. The monitor can be
// dead, or alive with every tick failing, while the gateway is fine: the
// book then has no touch or time-stop exits.
//
// Guarded or no open positions = renders nothing. A monitor waiting on the
// gateway is the GatewayBanner's alarm; this only notes it. A silent
// watchdog or an unreadable status never reads as healthy.

import { getExitMachine } from '../lib/api'
import { ago, etDateTime } from '../lib/format'
import { usePoll } from '../hooks/usePoll'
import type { ExitMachineStatus } from '../lib/types'
import { WATCH_STALE_S, watchAge } from '../lib/watch'

const HEADLINE: Record<string, string> = {
  monitor_down: 'Exit machine is not running',
  monitor_failing: 'Exit machine is running but its checks are failing',
  touch_blind: 'Touch exit is blind: no fresh underlying price',
}

// What the alarm costs the open positions.
function consequence(status: string): string {
  return status === 'touch_blind'
    ? "The touch exit can't fire until a price is back; time-stop and expiry exits still work"
    : 'Open positions have no touch or time-stop exits until it is back'
}

function sinceText(e: ExitMachineStatus, nowS: number): string {
  if (e.since === null) return ''
  const iso = new Date(e.since * 1000).toISOString()
  return `since ${etDateTime(iso)} (${ago(nowS - e.since)})`
}

function plans(e: ExitMachineStatus): string {
  const bad = e.books.filter((b) => HEADLINE[b.status]).map((b) => b.plan)
  return bad.length ? ` · ${bad.join(', ')}` : ''
}

function Unknown({ why, last }: { why: string; last: ExitMachineStatus | null }) {
  const lastBad = last ? HEADLINE[last.status] : undefined
  return (
    <div
      className={lastBad ? 'gateway-banner' : 'gateway-banner gateway-banner-muted'}
      role={lastBad ? 'alert' : 'status'}
    >
      {lastBad ? '⚠' : '○'} {why} — exit machine health unknown
      {lastBad && <span>last known: {lastBad}</span>}
    </div>
  )
}

export function ExitMachineBanner() {
  const poll = usePoll(getExitMachine, 30_000, 95_000)
  const e = poll.data
  const nowS = Date.now() / 1000

  if (poll.error) return <Unknown why="couldn't read the exit-machine watchdog" last={e} />
  if (!e) return null
  const age = watchAge(e, nowS)
  if (e.watch_stale || poll.isStale || age === null || age > WATCH_STALE_S) {
    const why =
      age !== null
        ? `exit-machine watchdog last reported ${ago(age)} ago`
        : 'exit-machine watchdog not reporting'
    return <Unknown why={why} last={e} />
  }

  const headline = HEADLINE[e.status]
  if (headline) {
    return (
      <div className="gateway-banner" role="alert">
        <strong>⚠ {headline}</strong>
        <span>
          {sinceText(e, nowS)}. {consequence(e.status)}
          {plans(e)}.
        </span>
      </div>
    )
  }
  if (e.status === 'touch_suspended') {
    // a blind incident the session ended without a price: not a recovery
    return (
      <div className="gateway-banner gateway-banner-muted" role="status">
        ○ Touch exit was blind at the close; waiting for a fresh price next session
      </div>
    )
  }
  if (e.status === 'waiting_for_gateway') {
    return (
      <div className="gateway-banner gateway-banner-muted" role="status">
        ○ Exit machine waiting for the IB Gateway
      </div>
    )
  }
  if (e.status !== 'ok' && e.status !== 'idle') {
    return <Unknown why="exit-machine watchdog has no verdict" last={e} />
  }
  return null
}
