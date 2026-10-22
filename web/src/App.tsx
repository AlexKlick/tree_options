import { Component, type ErrorInfo, type ReactNode } from 'react'
import { legacyRoute, useRoute, type CurrentRoute, type Route } from './lib/router'
import { DensityProvider } from './density'
import { ActivityPage, activitySectionFor } from './components/ActivityPage'
import { AppShell } from './components/AppShell'
import { ExperimentsPage } from './components/ExperimentsPage'
import { OnboardingPage } from './components/OnboardingPage'
import { PilotPage } from './components/PilotPage'
import { PortfolioPage } from './components/PortfolioPage'
import { TodayPage } from './components/TodayPage'
import { TradeReviewPage } from './components/TradeReviewPage'
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

type PageRender = (route: CurrentRoute) => ReactNode
const today: PageRender = () => <TodayPage />
const activity: PageRender = (route) => (
  <ActivityPage key={route.entry.id} section={activitySectionFor(route.entry.id)} />
)

// The Wave 3 pages by route id: lanes F (Today, Trade review), G (Portfolio,
// Activity & settings, acceptance, onboarding) and I (research pilot). Each
// page wraps itself in AppShell, which reads the route itself (nav, tab bar,
// Advanced menu, fallback notice, control bar). The route's parameter is
// `route.param`; a page keyed by it remounts when it changes.
export const WAVE3_PAGES: Readonly<Record<string, PageRender>> = {
  today,
  'trade-review': () => <TradeReviewPage />,
  'trade-review-proposal': (route) => <TradeReviewPage key={route.param} proposalId={route.param} />,
  portfolio: () => <PortfolioPage />,
  activity,
  'activity-acceptance': activity,
  'activity-settings': activity,
  onboarding: () => <OnboardingPage />,
  pilot: () => <PilotPage />,
  'pilot-program': (route) => <PilotPage key={route.param} programId={route.param ?? undefined} />,
}

/** A page that throws while rendering (a view shape it did not expect) shows
 * this inside the shell, so the nav and the control bar (Pause, Emergency
 * stop) stay reachable; it never blanks the whole app (integration risk). */
export class PageErrorBoundary extends Component<{ routeKey: string; children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidUpdate(previous: { routeKey: string }) {
    // another route gets a fresh try
    if (previous.routeKey !== this.props.routeKey && this.state.error !== null) this.setState({ error: null })
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.warn('page failed to render', error.message, info.componentStack?.split('\n')[1]?.trim() ?? '')
  }

  render() {
    if (this.state.error === null) return this.props.children
    return (
      <AppShell title="This page could not be shown" showRuntimeBanners={false}>
        <section className="card" role="alert">
          <p>
            The page met data it does not understand ({this.state.error.message}). Nothing was sent. The control bar
            above still works.
          </p>
          <p>
            <a href="#/today">Open Today</a>
          </p>
        </section>
      </AppShell>
    )
  }
}

function Page({ route }: { route: CurrentRoute }) {
  const legacy = legacyRoute(route)
  if (legacy !== null) return <LegacyPage route={legacy} />
  // unknown ids cannot reach here (resolveHash falls back to Today), but a
  // table edit that forgets one still renders Today, never a blank page
  const render = WAVE3_PAGES[route.entry.id] ?? today
  return <>{render(route)}</>
}

export default function App() {
  const route = useRoute()
  return (
    <DensityProvider>
      <PageErrorBoundary routeKey={route.hash}>
        <Page route={route} />
      </PageErrorBoundary>
    </DensityProvider>
  )
}
