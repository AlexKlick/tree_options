import type { ReactNode } from 'react'
import type { PollState } from '../hooks/usePoll'
import { DensityToggle } from './DensityToggle'
import { ExitMachineBanner } from './ExitMachineBanner'
import { GatewayBanner } from './GatewayBanner'
import { Pill } from './Pill'

// Transport health of the cockpit API only. Data freshness (marks,
// account, quotes) has its own age pills; "live" used to sit beside
// hours-old data and read as a freshness claim (M8 flash review).
function PollBadges({ poll }: { poll?: PollState<unknown> }) {
  if (!poll) return null
  if (poll.error && poll.data === null) {
    return <Pill variant="disarmed">● connection lost</Pill>
  }
  if (poll.error) {
    return <Pill variant="disarmed">● reconnecting</Pill>
  }
  if (poll.lastSuccessAt === null) {
    return <Pill variant="empty">○ loading</Pill>
  }
  if (poll.isStale) {
    return <Pill variant="empty">○ API slow</Pill>
  }
  return <Pill variant="armed">● API connected</Pill>
}

export function AppShell({
  title,
  poll,
  footerExtra,
  children,
}: {
  title: string
  poll?: PollState<unknown>
  footerExtra?: ReactNode
  children: ReactNode
}) {
  return (
    <div className="shell">
      <header>
        <div>
          <div className="eyebrow">trex · read-only cockpit</div>
          <h1>{title}</h1>
          <nav className="nav" aria-label="Sections">
            <a href="#/" aria-current={title === 'Plans' ? 'page' : undefined}>
              Plans
            </a>
            <a
              href="#/discover"
              aria-current={title === 'Discover' ? 'page' : undefined}
            >
              Discover
            </a>
            <a
              href="#/stats"
              aria-current={title === 'Performance' ? 'page' : undefined}
            >
              Performance
            </a>
            <a
              href="#/market"
              aria-current={title === 'Market' ? 'page' : undefined}
            >
              Market
            </a>
          </nav>
        </div>
        <div className="header-right">
          <PollBadges poll={poll} />
          <DensityToggle />
        </div>
      </header>
      <main>
        <GatewayBanner />
        <ExitMachineBanner />
        {children}
      </main>
      <footer>
        <span>
          broker-free · reads <code>~/.local/state/trex/&lt;plan&gt;/</code> + plan TOML
        </span>
        {footerExtra ? <span>{footerExtra}</span> : null}
      </footer>
    </div>
  )
}
