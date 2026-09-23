// IB Gateway health banner, on every page (AppShell). Fed by the
// trex-gateway-watch timer via GET /api/gateway. The 2026-09-22/23
// incident: the gateway sat logged out ~14h while every page looked
// normal apart from quietly aging marks.
//
// Healthy + watchdog reporting = renders nothing. A silent watchdog or an
// unreadable status never reads as healthy, and an old alarm is shown as
// last-known, not as current (Codex 2026-09-23).

import { getGateway } from '../lib/api'
import { ago, etDateTime } from '../lib/format'
import { usePoll } from '../hooks/usePoll'
import type { GatewayStatus } from '../lib/types'
import { WATCH_STALE_S, watchAge } from '../lib/watch'

const HEADLINE: Record<string, string> = {
  needs_login: 'IB Gateway needs a login',
  needs_2fa: 'IB Gateway is waiting for your 2FA approval in IBKR Mobile',
  api_down: "IB Gateway is logged in but its API isn't answering",
  down: 'IB Gateway container is not running',
}

function sinceText(g: GatewayStatus, nowS: number): string {
  if (g.since === null) return ''
  const iso = new Date(g.since * 1000).toISOString()
  return ` since ${etDateTime(iso)} (${ago(nowS - g.since)})`
}

function retryText(g: GatewayStatus): string | null {
  if (g.status !== 'needs_login' && g.status !== 'api_down') return null
  if (g.restarts_left === null) return null
  if (g.restarts_left <= 0) return 'auto-retry used up for today — log in manually'
  return `${g.restarts_left} auto-retr${g.restarts_left === 1 ? 'y' : 'ies'} left today`
}

function LoginLink({ g }: { g: GatewayStatus }) {
  if (!g.login_url || g.status === 'down') return null
  return (
    <a className="tap-link" href={g.login_url} target="_blank" rel="noopener noreferrer">
      Open the login screen →
    </a>
  )
}

// Health unknown: say why, and keep any previous alarm visible as last-known.
function Unknown({ why, last }: { why: string; last: GatewayStatus | null }) {
  const lastBad = last ? HEADLINE[last.status] : undefined
  return (
    <div
      className={lastBad ? 'gateway-banner' : 'gateway-banner gateway-banner-muted'}
      role={lastBad ? 'alert' : 'status'}
    >
      {lastBad ? '⚠' : '○'} {why} — gateway health unknown
      {lastBad && last && (
        <>
          <span>last known: {lastBad}</span>
          <LoginLink g={last} />
        </>
      )}
    </div>
  )
}

export function GatewayBanner() {
  const poll = usePoll(getGateway, 30_000, 95_000)
  const g = poll.data
  const nowS = Date.now() / 1000

  if (poll.error) return <Unknown why="couldn't read the gateway watchdog's status" last={g} />
  if (!g) return null
  const age = watchAge(g, nowS)
  if (g.watch_stale || poll.isStale || age === null || age > WATCH_STALE_S) {
    const why =
      age !== null
        ? `gateway watchdog last reported ${ago(age)} ago`
        : 'gateway watchdog not reporting'
    return <Unknown why={why} last={g} />
  }

  const headline = HEADLINE[g.status]
  if (headline) {
    const retry = retryText(g)
    return (
      <div className="gateway-banner" role="alert">
        <strong>⚠ {headline}</strong>
        <span>
          {sinceText(g, nowS).trim()}. The monitor can&apos;t mark or exit positions until it
          is back.
        </span>
        {retry && <span className="muted">{retry}</span>}
        <LoginLink g={g} />
      </div>
    )
  }
  if (g.status === 'checking') {
    return (
      <div className="gateway-banner gateway-banner-muted" role="status">
        ○ IB Gateway API not answering — rechecking{sinceText(g, nowS)}
      </div>
    )
  }
  if (g.status === 'starting') {
    return (
      <div className="gateway-banner gateway-banner-muted" role="status">
        ○ IB Gateway starting / logging in{sinceText(g, nowS)}
      </div>
    )
  }
  if (g.status !== 'ok') return <Unknown why="gateway watchdog has no verdict" last={g} />
  return null
}
