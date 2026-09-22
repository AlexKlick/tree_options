import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { ShadowBlock } from '../lib/types'
import { ShadowSection } from './ShadowSection'

afterEach(cleanup)

const shadow: ShadowBlock = {
  version: 1,
  positions: [
    {
      episode_id: 'NVDA|20261016|150|185#run-1',
      key: 'NVDA|20261016|150|185',
      underlying: 'NVDA',
      expiry: '20261016',
      short_strike: 150,
      long_strike: 185,
      width: 35,
      qty: 1,
      debit_paid: 0.21,
      opened_at: '2026-09-22T16:11:00-04:00',
      opened_run_id: 'run-1',
      status: 'open',
      last_mark: 0.35,
      last_mark_at: '2026-09-22T16:41:00-04:00',
      mark_source: 'scan',
      best_pnl: 14,
      worst_pnl: 14,
      final_pnl: null,
      pnl: 14,
    },
  ],
  stats: { open: 1, expired: 0, mean_pnl: 14, hit_rate: 1, not_executed: true },
  age_seconds: 30,
}

describe('ShadowSection', () => {
  it('renders rows with the not-executed disclosure and stats line', () => {
    render(<ShadowSection shadow={shadow} />)
    expect(screen.getByText(/forward-shadow · not executed/)).toBeTruthy()
    expect(screen.getByText('NVDA')).toBeTruthy()
    expect(screen.getByText('185/150')).toBeTruthy()
    expect(screen.getByText('scan row')).toBeTruthy()
    expect(screen.getAllByText('+$14').length).toBe(2) // P&L + best columns
    expect(screen.getByText(/1 open · 0 expired/)).toBeTruthy()
    expect(screen.getByText(/never executed at a broker/)).toBeTruthy()
  })

  it('discloses carried marks when a scan did not re-quote', () => {
    render(
      <ShadowSection
        shadow={{
          ...shadow,
          positions: [{ ...shadow.positions[0], mark_source: 'carry' }],
        }}
      />
    )
    expect(screen.getByText('carried (no re-quote)')).toBeTruthy()
  })

  it('renders nothing without a shadow book', () => {
    const { container } = render(<ShadowSection shadow={null} />)
    expect(container.textContent).toBe('')
  })
})
