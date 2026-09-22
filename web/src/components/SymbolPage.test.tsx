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
    source_as_of: '2026-09-22 22:08:55',
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
    expect(screen.getByText(((773.25 + 773.3) / 2).toFixed(2))).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Nvidia chips surge' })).toBeTruthy()
    expect(screen.getByText(/Reuters · Mon, 21 Sep 2026/)).toBeTruthy()
  })

  it('shows honest placeholders when caches are cold', async () => {
    mocked.mockResolvedValue({ ...detail, bars: null, news: [], quote: null })
    render(<SymbolPage sym="SPY" />)
    await waitFor(() => expect(screen.getByText(/No bars cached yet/)).toBeTruthy())
    expect(screen.getByText(/No news cached yet/)).toBeTruthy()
  })
})
