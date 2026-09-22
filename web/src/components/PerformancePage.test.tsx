import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { StatsResponse } from '../lib/types'
import { getStats } from '../lib/api'
import { PerformancePage } from './PerformancePage'

vi.mock('../lib/api', () => ({ getStats: vi.fn() }))
const mocked = vi.mocked(getStats)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const payload: StatsResponse = {
  now: '2026-09-22T18:00:00-04:00',
  tracking_since: '2026-09-22T12:00:00-04:00',
  equity: {
    points: [
      [1790088000000, 1000000],
      [1790091600000, 1000005],
    ],
    y_lo: 0,
    y_hi: 1000005,
    last: { ts_ms: 1790091600000, value: 1000005, pos: true },
  },
  days: [
    { date: '2026-09-22', realized: 28, unrealized_eod: -5, total: 23 },
    { date: '2026-09-23', realized: 0, unrealized_eod: null, total: null },
  ],
  totals: {
    realized: 28,
    unrealized_last: -5,
    wins: 1,
    losses: 0,
    win_rate: 1,
    best_day: 23,
    worst_day: 23,
    plans_tracked: 1,
    structures_closed: 0,
  },
  per_plan: [
    {
      plan_id: 'putspread-test',
      realized: 28,
      unrealized_last: -5,
      structures_closed: 0,
      first_ts: '2026-09-22T10:00:00-04:00',
      last_ts: '2026-09-22T15:00:00-04:00',
    },
  ],
  per_structure: [
    {
      plan_id: 'putspread-test',
      structure_id: 'nvda-oct',
      underlying: 'NVDA',
      status: 'exit_working',
      entry_fill: 0.21,
      filled_qty: 5,
      realized: 28,
    },
  ],
}

describe('PerformancePage', () => {
  it('renders equity chart, day table with gaps as em-dash, and breakdowns', async () => {
    mocked.mockResolvedValue(payload)
    render(<PerformancePage />)
    await waitFor(() => expect(screen.getByText('Equity')).toBeTruthy())
    expect(screen.getByRole('img', { name: /net liquidation over time/i })).toBeTruthy()
    expect(screen.getByText('2026-09-23')).toBeTruthy()
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2) // gap day
    expect(screen.getAllByText('+$28').length).toBeGreaterThanOrEqual(2) // tile + day row
    expect(screen.getByText('nvda-oct')).toBeTruthy()
    expect(screen.getByText('1W / 0L · 100%')).toBeTruthy()
  })

  it('labels the two bases and tracking start', async () => {
    mocked.mockResolvedValue(payload)
    render(<PerformancePage />)
    await waitFor(() => expect(screen.getByText(/tracking began 2026-09-22/)).toBeTruthy())
    expect(screen.getByText(/equity = net liquidization/)).toBeTruthy()
  })

  it('renders honest empty state before history exists', async () => {
    mocked.mockResolvedValue({
      ...payload,
      equity: null,
      days: [],
      tracking_since: null,
    })
    render(<PerformancePage />)
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Plans' })).toBeTruthy(),
    )
    expect(screen.getByText(/Equity tracking begins/)).toBeTruthy()
    expect(screen.getByText(/No marks history yet/)).toBeTruthy()
  })
})
