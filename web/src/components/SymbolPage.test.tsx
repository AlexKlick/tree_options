import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SymbolDetail } from '../lib/types'
import { getSymbol } from '../lib/api'
import { SymbolPage } from './SymbolPage'

vi.mock('../lib/api', () => ({ getSymbol: vi.fn(), requestMarketRefresh: vi.fn() }))
const mocked = vi.mocked(getSymbol)

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

describe('SymbolPage', () => {
  it('renders quote tiles, chart with date axis, and news links', async () => {
    mocked.mockResolvedValue(detail)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText('Quote (mid)')).toBeTruthy())
    expect(screen.getByRole('img', { name: /Daily closes for SPY/ })).toBeTruthy()
    expect(screen.getByText(/source 18:08 ET/)).toBeTruthy()
    expect(screen.queryByText(/data .* old/)).toBeNull() // no ages -> no note
    expect(screen.getByText(((773.25 + 773.3) / 2).toFixed(2))).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Nvidia chips surge' })).toBeTruthy()
    expect(screen.getByText(/Reuters · Mon, 21 Sep 2026/)).toBeTruthy()
  })

  it('discloses stale cache ages instead of hiding the data', async () => {
    mocked.mockResolvedValue({ ...detail, bars_age_seconds: 3 * 3600, news_age_seconds: 2400 })
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText(/data 3h old/)).toBeTruthy())
    expect(screen.getByText(/data 40m old/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Nvidia chips surge' })).toBeTruthy()
  })

  it('opts the back link into the 40px mobile tap target (M8 flash review: 25px)', async () => {
    mocked.mockResolvedValue(detail)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText('Quote (mid)')).toBeTruthy())
    expect(screen.getByRole('link', { name: '← Market' }).className).toContain('tap-link')
  })

  it('gives IV30 its percent unit (M8 flash review: bare "11.4")', async () => {
    mocked.mockResolvedValue(detail)
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText('11.4%')).toBeTruthy())
  })

  it('shows honest placeholders when caches are cold', async () => {
    mocked.mockResolvedValue({ ...detail, bars: null, news: [], quote: null })
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText(/No bars cached yet/)).toBeTruthy())
    expect(screen.getByText(/No news cached yet/)).toBeTruthy()
  })
})
