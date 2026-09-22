import type { ReactNode } from 'react'
import type { PollState } from '../hooks/usePoll'
import { DensityToggle } from './DensityToggle'
import { Pill } from './Pill'

function PollBadges({ poll }: { poll?: PollState<unknown> }) {
  if (!poll) return null
  if (poll.error && poll.data === null) {
    return <Pill variant="disarmed">● connection lost</Pill>
  }
  if (poll.error) {
    return <Pill variant="disarmed">● reconnecting</Pill>
  }
  if (poll.isStale) {
    return <Pill variant="empty">○ stale</Pill>
  }
  return <Pill variant="armed">● live</Pill>
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
          </nav>
        </div>
        <div className="header-right">
          <PollBadges poll={poll} />
          <DensityToggle />
        </div>
      </header>
      <main>{children}</main>
      <footer>
        <span>
          broker-free · reads <code>~/.local/state/trex/&lt;plan&gt;/</code> + plan TOML
        </span>
        {footerExtra ? <span>{footerExtra}</span> : null}
      </footer>
    </div>
  )
}
