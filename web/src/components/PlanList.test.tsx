import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PlansResponse } from '../lib/types'
import { getPlans } from '../lib/api'
import { PlanList } from './PlanList'

// getGateway/getExitMachine feed AppShell's health banners (tested on its own); never settles here
vi.mock('../lib/api', () => ({
  getPlans: vi.fn(),
  getGateway: () => new Promise(() => {}),
  getExitMachine: () => new Promise(() => {}),
}))
const mockedGetPlans = vi.mocked(getPlans)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const payload: PlansResponse = {
  now: '2026-09-22T17:30:00-04:00',
  gateway_reachable: true,
  plans: [
    {
      id: 'putspread-test',
      account_mode: 'paper',
      entry_date: '2026-09-18',
      entry_window_start: '09:45',
      entry_window_end: '12:00',
      structure_count: 2,
      total_debit_cap: 1840,
      committed_at_caps: 1840,
      state_present: true,
      armed: true,
      heartbeat: '2026-09-22T17:29:00-04:00',
      worst_state: 'open',
      window_state: 'after',
      open_qty: 5,
      days_to_expiry: { 'nvda-oct': 24 },
      days_to_deadline: { 'nvda-oct': 17 },
      unrealized_open: -5,
      unrealized_filled: -5,
      realized: null,
    },
  ],
  portfolio: {
    plans_count: 1,
    plans_with_state: 1,
    open_qty: 5,
    committed_at_caps: 1840,
    committed_filled: 105,
  committed_known: 105,
  cost_unknown: false,
  unpriced_qty: 0,
    unrealized_open: -5,
    unrealized_filled: -5,
    realized: null,
    realized_partial_count: 0,
    marks_age_seconds: 12,
    marks_stale: false,
    worst_state: 'open',
    by_account_mode: {},
  },
  net_positions: [
    {
      underlying: 'NVDA',
      structure_count: 1,
      open_qty: 5,
      avg_entry: 0.21,
      committed: 105,
      committed_known: 105,
      cost_unknown: false,
      unpriced_qty: 0,
      max_gain: 1645,
      max_loss: -105,
      unrealized: -5,
      short_floor: 150,
      long_ceiling: 185,
      legs: [
        {
          structure_id: 'nvda-oct',
          expiry: '2026-10-16',
          long_strike: 185,
          short_strike: 150,
          open_qty: 5,
          entry: 0.21,
          entry_unpriced_qty: 0,
        },
      ],
    },
  ],
  account: null,
  accounts_seen: [],
}

describe('PlanList', () => {
  it('shows current positions on the index without clicking into a plan', async () => {
    mockedGetPlans.mockResolvedValue(payload)
    render(<PlanList />)
    await waitFor(() => expect(screen.getByText('Current positions')).toBeTruthy())
    expect(screen.getByText('NVDA')).toBeTruthy()
    expect(screen.getByText(/nvda-oct/)).toBeTruthy()
    expect(screen.getByText(/185\/150/)).toBeTruthy()
  })

  it('labels plan cards with an open affordance', async () => {
    mockedGetPlans.mockResolvedValue(payload)
    render(<PlanList />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /putspread-test/ })).toBeTruthy(),
    )
    expect(screen.getByText(/open plan →/)).toBeTruthy()
  })

  it('omits the positions section when the API has none (restart window)', async () => {
    mockedGetPlans.mockResolvedValue({ ...payload, net_positions: null })
    render(<PlanList />)
    await waitFor(() => expect(screen.getByText('putspread-test')).toBeTruthy())
    expect(screen.queryByText('Current positions')).toBeNull()
  })
})
