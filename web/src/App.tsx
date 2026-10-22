import type { ReactNode } from 'react'
import { legacyRoute, useRoute, type CurrentRoute, type Route } from './lib/router'
import { DensityProvider } from './density'
import { AppShell } from './components/AppShell'
import { ExperimentsPage } from './components/ExperimentsPage'
import { DiscoverPage } from './components/DiscoverPage'
import { PerformancePage } from './components/PerformancePage'
import { PlanDetail } from './components/PlanDetail'
import { MarketPage } from './components/MarketPage'
import { SymbolPage } from './components/SymbolPage'
import { PlanList } from './components/PlanList'
import { ResearchPage } from './components/ResearchPage'
import { ActionModelPage } from './components/ActionModelPage'
import { QuantPage } from './components/QuantPage'
import { ResearchWorkspacePage } from './components/ResearchWorkspacePage'
import { TradeFloorPage } from './components/TradeFloorPage'

// Routing on the Wave 3 table (lib/routes.ts, C3.3): '#/' and unknown hashes
// open Today, every pre-Wave-3 hash still opens its old view (the table's
// legacyView column), and the new pages are chosen by route id.

/** A pre-Wave-3 view, rendered exactly as before Wave 3. */
function LegacyPage({ route }: { route: Route }) {
  switch (route.view) {
    case 'experiments':
      return <ExperimentsPage id={route.id} />
    case 'plan':
      return <PlanDetail key={route.id} id={route.id} />
    case 'discover':
      return <DiscoverPage />
    case 'stats':
      return <PerformancePage />
    case 'market':
      return <MarketPage />
    case 'symbol':
      return <SymbolPage key={route.id} sym={route.id} />
    case 'research':
      return <ResearchPage />
    case 'action-model':
      return <ActionModelPage />
    case 'quant':
      return <QuantPage />
    case 'workspace':
      return <ResearchWorkspacePage />
    case 'trade-floor':
      return <TradeFloorPage />
    case 'list':
      return <PlanList />
  }
}

/** Stands in for a Wave 3 page that is not on this branch yet. */
function PageStub({ route }: { route: CurrentRoute }) {
  return (
    <AppShell title={route.entry.label}>
      <section className="card">
        <p className="muted">Loading {route.entry.label}: this page is not in this build yet.</p>
      </section>
    </AppShell>
  )
}

type PageRender = (route: CurrentRoute) => ReactNode
const stub: PageRender = (route) => <PageStub route={route} />

// The Wave 3 pages by route id. Their components come from lanes F (Today,
// Trade review), G (Portfolio, Activity & settings, acceptance, onboarding)
// and I (research pilot); none exists on wave3/base, so each is a stub here
// and integration swaps in the component, one entry per page (the route's
// parameter is `route.param`).
export const WAVE3_PAGES: Readonly<Record<string, PageRender>> = {
  today: stub,
  'trade-review': stub,
  'trade-review-proposal': stub,
  portfolio: stub,
  activity: stub,
  'activity-acceptance': stub,
  'activity-settings': stub,
  onboarding: stub,
  pilot: stub,
  'pilot-program': stub,
}

function Page({ route }: { route: CurrentRoute }) {
  const legacy = legacyRoute(route)
  if (legacy !== null) return <LegacyPage route={legacy} />
  const render = WAVE3_PAGES[route.entry.id] ?? stub
  return <>{render(route)}</>
}

export default function App() {
  const route = useRoute()
  return (
    <DensityProvider>
      <Page route={route} />
    </DensityProvider>
  )
}
