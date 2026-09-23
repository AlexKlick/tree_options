import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ScenarioDoc } from '../lib/types'
import { getScenario, requestScenario } from '../lib/api'
import { ScenarioPanel } from './ScenarioPanel'

vi.mock('../lib/api', () => ({ getScenario: vi.fn(), requestScenario: vi.fn() }))
const mockedGet = vi.mocked(getScenario)
const mockedPost = vi.mocked(requestScenario)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const KEY = 'QQQ|20261016|642|657'
const stats = {
  count: 96, wins: 12, win_rate: 0.125, mean_pnl: -8.4, median_pnl: -19.5,
  p10_pnl: -19.5, worst_pnl: -19.5, best_pnl: 1480.5,
}
const doc: ScenarioDoc = {
  key: KEY,
  generated_at: new Date(Date.now() + 60_000).toISOString(), // newer than the request
  label: 'valuation scenario · approximate · simulated',
  age_seconds: 1,
  error: null,
  structure: {
    underlying: 'QQQ', expiry: '20261016', short: 642, long: 657, dte: 24,
    debit_mid: 0.195, debit_ask: 0.23, spot_now: 748, found_in: 'latest scan',
  },
  assumptions: { exits_modeled: 'none (buy-and-hold to analog expiry)', iv: 0.3123 },
  iv: 0.3123,
  iv_source: "calibrated to today's quoted debit_mid (look-ahead)",
  analogs: stats,
  pessimistic: { ...stats, win_rate: 0.1, mean_pnl: -11.9 },
  iv_band_mean_pnl: { lo: 2.1, hi: -14.0 },
  recent: {
    entry_ms: 1789992000000,
    entry_spot: 731.2,
    entry_debit: 0.21,
    final_pnl: -21,
    series: {
      points: [[1789992000000, -3], [1790078400000, -21]],
      y_lo: -25, y_hi: 0,
      last: { ts_ms: 1790078400000, value: -21, pos: false },
    },
  },
  sessions: 250,
}

describe('ScenarioPanel', () => {
  it('requests by key, labels the result simulated, and discloses assumptions', async () => {
    mockedPost.mockResolvedValue({ accepted: true, request_id: 'r1', key: KEY })
    mockedGet.mockResolvedValue(doc)
    render(<ScenarioPanel scenarioKey={KEY} title="QQQ 657/642" onClose={() => {}} />)
    expect(mockedPost).toHaveBeenCalledWith(KEY)
    await waitFor(() => expect(screen.getByText(/96 windows/)).toBeTruthy())
    expect(screen.getByText('valuation scenario · approximate · simulated')).toBeTruthy()
    expect(screen.getByText('not counted in real win rates')).toBeTruthy()
    expect(screen.getByText(/96 windows · win 13%/)).toBeTruthy()
    expect(screen.getByText(/paying today's ask on every entry: win 10%/)).toBeTruthy()
    expect(screen.getByText(/paying today's ask/)).toBeTruthy()
    expect(screen.getByRole('img', { name: /Simulated P&L path/ })).toBeTruthy()
    expect(screen.getByText(/assumptions/)).toBeTruthy()
    expect(screen.queryByText(/runner computing/)).toBeNull()
  })

  it('shows the runner error instead of spinning', async () => {
    mockedPost.mockResolvedValue({ accepted: true, request_id: 'r2', key: KEY })
    mockedGet.mockResolvedValue({
      key: KEY, generated_at: doc.generated_at, label: doc.label, age_seconds: 0,
      error: 'no QQQ quote cached (refresh the market desk first)',
    })
    render(<ScenarioPanel scenarioKey={KEY} title="QQQ 657/642" onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText(/Scenario unavailable: no QQQ quote/)).toBeTruthy())
  })

  it('marks a pre-existing artifact as still computing', async () => {
    mockedPost.mockResolvedValue({ accepted: true, request_id: 'r3', key: KEY })
    mockedGet.mockResolvedValue({ ...doc, generated_at: '2026-01-01T00:00:00Z' })
    render(<ScenarioPanel scenarioKey={KEY} title="QQQ 657/642" onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText(/96 windows/)).toBeTruthy())
    expect(screen.getByText(/runner computing/)).toBeTruthy()
  })
})
