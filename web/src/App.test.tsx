import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

// Every pre-Wave-3 page is replaced by a marker naming the component and
// its props, so the test sees which view a hash opens without its data.
vi.mock('./components/PlanList', () => ({ PlanList: () => <p>view:PlanList</p> }))
vi.mock('./components/PlanDetail', () => ({ PlanDetail: ({ id }: { id: string }) => <p>view:PlanDetail:{id}</p> }))
vi.mock('./components/DiscoverPage', () => ({ DiscoverPage: () => <p>view:DiscoverPage</p> }))
vi.mock('./components/PerformancePage', () => ({ PerformancePage: () => <p>view:PerformancePage</p> }))
vi.mock('./components/MarketPage', () => ({ MarketPage: () => <p>view:MarketPage</p> }))
vi.mock('./components/SymbolPage', () => ({ SymbolPage: ({ sym }: { sym: string }) => <p>view:SymbolPage:{sym}</p> }))
vi.mock('./components/ResearchPage', () => ({ ResearchPage: () => <p>view:ResearchPage</p> }))
vi.mock('./components/ActionModelPage', () => ({ ActionModelPage: () => <p>view:ActionModelPage</p> }))
vi.mock('./components/QuantPage', () => ({ QuantPage: () => <p>view:QuantPage</p> }))
vi.mock('./components/ResearchWorkspacePage', () => ({ ResearchWorkspacePage: () => <p>view:ResearchWorkspacePage</p> }))
vi.mock('./components/TradeFloorPage', () => ({ TradeFloorPage: () => <p>view:TradeFloorPage</p> }))
vi.mock('./components/ExperimentsPage', () => ({
  ExperimentsPage: ({ id }: { id?: string }) => <p>view:ExperimentsPage:{id ?? '-'}</p>,
}))

// Hand-written alias table: the hash, the component it opened before
// Wave 3, and the props it got.
const OLD_VIEWS: Array<[string, string]> = [
  ['#/plans', 'view:PlanList'],
  ['#/plan/spy-put-spread', 'view:PlanDetail:spy-put-spread'],
  ['#/plan/a%20b', 'view:PlanDetail:a b'],
  ['#/stats', 'view:PerformancePage'],
  ['#/market', 'view:MarketPage'],
  ['#/market/SPY', 'view:SymbolPage:SPY'],
  ['#/discover', 'view:DiscoverPage'],
  ['#/research', 'view:ResearchPage'],
  ['#/action-model', 'view:ActionModelPage'],
  ['#/quant', 'view:QuantPage'],
  ['#/workspace', 'view:ResearchWorkspacePage'],
  ['#/trade-floor', 'view:TradeFloorPage'],
  ['#/experiments', 'view:ExperimentsPage:-'],
  ['#/experiments/qsl:observation', 'view:ExperimentsPage:qsl:observation'],
]

// Wave 3 pages and the heading each shows (stubs until lanes F, G and I land).
const NEW_PAGES: Array<[string, string]> = [
  ['', 'Today'],
  ['#/', 'Today'],
  ['#/today', 'Today'],
  ['#/trade-review', 'Trade review'],
  [`#/trade-review/${'b'.repeat(64)}`, 'Proposal'],
  ['#/portfolio', 'Portfolio'],
  ['#/activity', 'Activity & settings'],
  ['#/activity/acceptance', 'Paper acceptance'],
  ['#/activity/settings', 'Settings'],
  ['#/pilot', 'Research pilot'],
  ['#/pilot/eq-baseline-spy', 'Pilot program'],
  ['#/onboarding', 'Getting started'],
  ['#/no-such-page', 'Today'],
]

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 404 })))
})

afterEach(() => {
  vi.unstubAllGlobals()
  history.replaceState(null, '', '#')
})

describe('App routing', () => {
  it.each(OLD_VIEWS)('%s still opens its old view', (hash, marker) => {
    history.replaceState(null, '', hash)
    render(<App />)
    expect(screen.getByText(marker)).toBeTruthy()
  })

  it.each(NEW_PAGES)('%s opens the %s page', (hash, heading) => {
    history.replaceState(null, '', hash)
    render(<App />)
    expect(screen.getByRole('heading', { level: 1, name: heading })).toBeTruthy()
    expect(screen.queryByText(/^view:/)).toBeNull()
  })

  it('the root no longer opens the Plans list', () => {
    history.replaceState(null, '', '#/')
    render(<App />)
    expect(screen.queryByText('view:PlanList')).toBeNull()
  })

  it('follows hash changes', async () => {
    history.replaceState(null, '', '#/plans')
    render(<App />)
    expect(screen.getByText('view:PlanList')).toBeTruthy()
    await act(async () => {
      history.replaceState(null, '', '#/market/SPY')
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(screen.getByText('view:SymbolPage:SPY')).toBeTruthy()
    expect(screen.queryByText('view:PlanList')).toBeNull()
  })
})
