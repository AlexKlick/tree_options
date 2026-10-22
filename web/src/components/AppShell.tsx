import type { ReactNode } from 'react'
import type { PollState } from '../hooks/usePoll'
import { useMediaQuery } from '../hooks/useMediaQuery'
import { useRoute, type CurrentRoute } from '../lib/router'
import { DESTINATIONS, advancedFor, hashFor, type Destination } from '../lib/routes'
import { ControlBar } from './ControlBar'
import { DensityToggle } from './DensityToggle'
import { ExitMachineBanner } from './ExitMachineBanner'
import { GatewayBanner } from './GatewayBanner'
import { Pill } from './Pill'
import './AppShell.css'

// The Wave 3 shell (C3.3): five destinations as a desktop nav or, on a phone,
// a bottom tab bar; the pre-Wave-3 views in an Advanced menu under their
// destination; the desk control bar on every page. Every page still renders
// its own AppShell; the shell reads the route itself, so old pages need no
// change. Links are hash-only (the app works under / and the portal's /trex/).

/** Phones get the bottom tab bar (the stylesheet's narrow breakpoint). */
export const PHONE_QUERY = '(max-width: 720px)'

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

/** `page` on the destination's own route, `true` on a page under it. */
function currentMark(route: CurrentRoute, destination: Destination): 'page' | 'true' | undefined {
  if (route.destination !== destination.id) return undefined
  return route.entry.id === destination.id ? 'page' : 'true'
}

function DestinationNav({ route }: { route: CurrentRoute }) {
  return (
    <nav className="nav nav-destinations" aria-label="Destinations">
      {DESTINATIONS.map((d) => (
        <a key={d.id} href={d.hash} aria-current={currentMark(route, d)}>
          {d.label}
        </a>
      ))}
    </nav>
  )
}

function TabBar({ route }: { route: CurrentRoute }) {
  return (
    <nav className="tabbar" aria-label="Tabs">
      {DESTINATIONS.map((d) => (
        <a key={d.id} href={d.hash} aria-current={currentMark(route, d)}>
          <span className="tabbar-dot" aria-hidden="true" />
          <span className="tabbar-label">{d.shortLabel}</span>
        </a>
      ))}
    </nav>
  )
}

function AdvancedMenu({ route }: { route: CurrentRoute }) {
  const items = advancedFor(route.destination)
  if (items.length === 0) return null
  // open while one of its views (or a page under one, e.g. a plan) is showing
  const inside = route.entry.group === 'advanced'
  return (
    <nav className="advanced-nav" aria-label="Advanced views">
      <details open={inside}>
        <summary>Advanced</summary>
        <ul>
          {items.map((item) => (
            <li key={item.id}>
              <a href={hashFor(item.id)} aria-current={route.entry.id === item.id ? 'page' : undefined}>
                {item.label}
              </a>
            </li>
          ))}
        </ul>
      </details>
    </nav>
  )
}

export function AppShell({
  title,
  poll,
  footerExtra,
  footerSource,
  showRuntimeBanners = true,
  contextLabel = 'trex · paper desk',
  children,
}: {
  title: string
  poll?: PollState<unknown>
  footerExtra?: ReactNode
  footerSource?: ReactNode
  showRuntimeBanners?: boolean
  contextLabel?: string
  children: ReactNode
}) {
  const route = useRoute()
  const phone = useMediaQuery(PHONE_QUERY)
  return (
    <div className={phone ? 'shell shell-phone' : 'shell'}>
      <header>
        <div className="shell-head">
          <div className="eyebrow">
            {contextLabel} <span className="paper-only">Paper only</span>
          </div>
          <h1>{title}</h1>
          {phone ? null : <DestinationNav route={route} />}
          <AdvancedMenu route={route} />
        </div>
        <div className="header-right">
          <PollBadges poll={poll} />
          <DensityToggle />
        </div>
      </header>
      <ControlBar compact={phone} />
      <main>
        {route.fallback ? (
          <p className="route-fallback muted">
            No page at <code>{route.hash}</code>; showing Today.
          </p>
        ) : null}
        {showRuntimeBanners && <GatewayBanner />}
        {showRuntimeBanners && <ExitMachineBanner />}
        {children}
      </main>
      <footer>
        <span>{footerSource ?? <>
          broker-free · reads <code>~/.local/state/trex/&lt;plan&gt;/</code> + plan TOML
        </>}</span>
        {footerExtra ? <span>{footerExtra}</span> : null}
      </footer>
      {phone ? <TabBar route={route} /> : null}
    </div>
  )
}
