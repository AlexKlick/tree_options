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

const HEADLINE: Record<string, string> = {
  monitor_down: 'Exit machine is not running',
  monitor_failing: 'Exit machine is running but its checks are failing',
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
  const poll = usePoll(getExitMachine, 30_000)
  const e = poll.data
  const nowS = Date.now() / 1000

  if (poll.error) return <Unknown why="couldn't read the exit-machine watchdog" last={e} />
  if (!e) return null
  if (e.watch_stale) {
    const why =
      e.age_seconds !== null
        ? `exit-machine watchdog last reported ${ago(e.age_seconds)} ago`
        : 'exit-machine watchdog not reporting'
    return <Unknown why={why} last={e} />
  }

  const headline = HEADLINE[e.status]
  if (headline) {
    return (
      <div className="gateway-banner" role="alert">
        <strong>⚠ {headline}</strong>
        <span>
          {sinceText(e, nowS)}. Open positions have no touch or time-stop exits until it is
          back{plans(e)}.
        </span>
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
  return null
}
