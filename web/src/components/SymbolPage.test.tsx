import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SymbolDetail, SymbolHistory } from '../lib/types'
import { getSymbol, getSymbolHistory } from '../lib/api'
import { SymbolPage } from './SymbolPage'

// getGateway/getExitMachine feed AppShell's health banners (tested on its own); never settles here
vi.mock('../lib/api', () => ({
  getSymbol: vi.fn(),
  getSymbolHistory: vi.fn(),
  requestMarketRefresh: vi.fn(),
  getGateway: () => new Promise(() => {}),
  getExitMachine: () => new Promise(() => {}),
}))
const mocked = vi.mocked(getSymbol)
const mockedHistory = vi.mocked(getSymbolHistory)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const detail: SymbolDetail = {
  now: '2026-09-22T19:30:00-04:00',
  symbol: 'SPY',
  quote: {
    bid: 773.25,
    ask: 773.3,
    close: 773.38,
    iv30: 11.431,
    change_pct: -0.42,
    source_as_of: '2026-09-22T22:08:55+00:00',
  },
  quote_age_seconds: null,
  bars: {
    points: [
      [1789992000000, 750.1],
      [1790078400000, 757.67],
    ],
    y_lo: 0,
    y_hi: 757.67,
    last: { ts_ms: 1790078400000, value: 757.67, pos: true },
  },
  news: [{ title: 'Nvidia chips surge', link: 'https://example.com/a', pub: 'Mon, 21 Sep 2026', source: 'Reuters' }],
}

const history: SymbolHistory = {
  now: '2026-09-22T19:30:00-04:00',
  symbol: 'SPY',
  source: 'ohlc-panel',
  in_panel: true,
  panel_last_session: '2026-09-22',
  panel_sha256_12: 'abc123def456',
  range: '3y',
  range_start: '2023-09-25',
  range_sessions: 754,
  points: [
    [1695643200000, 449.1, 451.2, 448.0, 450.8, 89_000_000],
    [1790049600000, 560.2, 566.0, 558.9, 565.9, 94_000_000],
  ],
  y_lo: 450.8,
  y_hi: 565.9,
  vol_max: 94_000_000,
  last: { date: '2026-09-22', ts_ms: 1790049600000, close: 565.9 },
  history_age_seconds: null,
  note: 'split-adjusted daily OHLCV · ohlc-panel.json · extended nightly by desk-eod-equity',
  error: null,
}

// PLTR/SPCX pre-backfill shape: honest empty panel, legacy fallback kicks in
const notInPanel: SymbolHistory = {
  ...history,
  in_panel: false,
  panel_last_session: null,
  panel_sha256_12: null,
  range_start: undefined,
  range_sessions: undefined,
  points: [],
  y_lo: null,
  y_hi: null,
  vol_max: null,
  last: null,
}

describe('SymbolPage', () => {
  it('renders quote tiles, tabbed views, and the panel-backed price chart', async () => {
    mocked.mockResolvedValue(detail)
    mockedHistory.mockResolvedValue(history)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText('Quote (mid)')).toBeTruthy())

    // tabs: Price is the default view
    expect(screen.getByRole('tab', { name: 'Price' }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByRole('tab', { name: 'Options' })).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Ideas' })).toBeTruthy()

    // range chips + renderer toggle + the new chart
    for (const label of ['1Y', '3Y', '5Y', 'Max']) {
      expect(screen.getByRole('button', { name: label })).toBeTruthy()
    }
    expect(screen.getByRole('button', { name: 'Line' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Candles' })).toBeTruthy()
    expect(screen.getByRole('img', { name: /Price history for SPY/ })).toBeTruthy()
    expect(screen.getByText(/panel through 2026-09-22 · split-adjusted · nightly · 754 sessions/)).toBeTruthy()

    // quote + news still intact
    expect(screen.getByText(/source 18:08 ET/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Nvidia chips surge' })).toBeTruthy()
  })

  it('refetches at candle point count when the renderer toggles', async () => {
    mocked.mockResolvedValue(detail)
    mockedHistory.mockResolvedValue(history)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByRole('img', { name: /Price history for SPY/ })).toBeTruthy())
    expect(mockedHistory).toHaveBeenLastCalledWith('SPY', '3y', 600)

    fireEvent.click(screen.getByRole('button', { name: 'Candles' }))
    await waitFor(() => expect(mockedHistory).toHaveBeenLastCalledWith('SPY', '3y', 160))

    fireEvent.click(screen.getByRole('button', { name: '5Y' }))
    await waitFor(() => expect(mockedHistory).toHaveBeenLastCalledWith('SPY', '5y', 160))
  })

  it('fills the Options and Ideas tabs with honest placeholders', async () => {
    mocked.mockResolvedValue(detail)
    mockedHistory.mockResolvedValue(history)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Options' })).toBeTruthy())

    fireEvent.click(screen.getByRole('tab', { name: 'Options' }))
    expect(screen.getByText('Options surface lands in the next change')).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'Ideas' }))
    expect(screen.getByText('Ideas panel lands in the next change')).toBeTruthy()
  })

  it('falls back to the legacy envelope chart when the panel has no history', async () => {
    mocked.mockResolvedValue(detail)
    mockedHistory.mockResolvedValue(notInPanel)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() =>
      expect(screen.getByRole('img', { name: /Daily closes for SPY/ })).toBeTruthy(),
    )
    expect(screen.getByText(/~1 year · Polygon delayed/)).toBeTruthy()
  })

  it('shows an honest busy card with retry when the panel degrades', async () => {
    mocked.mockResolvedValue(detail)
    mockedHistory.mockResolvedValue({ ...history, points: null, error: 'panel busy: writer holds the lock' })
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText(/panel busy: writer holds the lock/)).toBeTruthy())
    expect(mockedHistory).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(mockedHistory).toHaveBeenCalledTimes(2))
  })

  it('discloses stale cache ages instead of hiding the data', async () => {
    mocked.mockResolvedValue({ ...detail, bars_age_seconds: 3 * 3600, news_age_seconds: 2400 })
    mockedHistory.mockResolvedValue(notInPanel)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText(/data 3h old/)).toBeTruthy())
    expect(screen.getByText(/data 40m old/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Nvidia chips surge' })).toBeTruthy()
  })

  it('opts the back link into the 40px mobile tap target (M8 flash review: 25px)', async () => {
    mocked.mockResolvedValue(detail)
    mockedHistory.mockResolvedValue(history)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText('Quote (mid)')).toBeTruthy())
    expect(screen.getByRole('link', { name: '← Market' }).className).toContain('tap-link')
  })

  it('gives IV30 its percent unit (M8 flash review: bare "11.4")', async () => {
    mocked.mockResolvedValue(detail)
    mockedHistory.mockResolvedValue(history)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText('11.4%')).toBeTruthy())
  })

  it('shows honest placeholders when caches are cold', async () => {
    mocked.mockResolvedValue({ ...detail, bars: null, news: [], quote: null })
    mockedHistory.mockResolvedValue(notInPanel)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText(/No bars cached yet/)).toBeTruthy())
    expect(screen.getByText(/No news cached yet/)).toBeTruthy()
  })
})
