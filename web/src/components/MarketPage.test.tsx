import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MarketResponse } from '../lib/types'
import { getMarket } from '../lib/api'
import { MarketPage } from './MarketPage'

vi.mock('../lib/api', () => ({ getMarket: vi.fn() }))
const mocked = vi.mocked(getMarket)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const payload: MarketResponse = {
  now: '2026-09-22T19:00:00-04:00',
  last_refresh: '2026-09-22T18:59:30-04:00',
  age_seconds: 30,
  watchlist: ['SPY'],
  symbols: {
    SPY: {
      bid: 773.25,
      ask: 773.3,
      close: 773.38,
      iv30: 11.431,
      change_pct: -0.42,
      source_as_of: '2026-09-22T22:08:55+00:00',
    },
  },
  errors: {},
}

describe('MarketPage', () => {
  it('renders quote cards with data-age pills and source times', async () => {
    mocked.mockResolvedValue(payload)
    render(<MarketPage />)
    await waitFor(() => expect(screen.getByText('SPY')).toBeTruthy())
    expect(screen.getByText('-0.42%')).toBeTruthy()
    expect(screen.getByText(((773.25 + 773.3) / 2).toFixed(2))).toBeTruthy() // mid
    expect(screen.getByText(/iv30 11.4/)).toBeTruthy()
    expect(screen.getByText(/source 18:08 ET/)).toBeTruthy() // 22:08:55Z -> ET
    expect(screen.getByText('● 30s old')).toBeTruthy()
  })

  it('renders honest empty state and disclosed errors', async () => {
    mocked.mockResolvedValue({ ...payload, symbols: {}, errors: { QQQ: 'HTTPError: boom' } })
    render(<MarketPage />)
    await waitFor(() => expect(screen.getByText('No market snapshot yet')).toBeTruthy())
    expect(screen.getByText(/QQQ: HTTPError: boom/)).toBeTruthy()
  })
})
