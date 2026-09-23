import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MarksTable } from './MarksTable'
import type { Marks, PlanStructureSpec } from '../lib/types'

const specs: PlanStructureSpec[] = [
  {
    id: 'nvda-oct',
    underlying: 'NVDA',
    entry_date: '2026-09-22',
    expiry: '2026-10-16',
    exit_deadline: '2026-10-09',
    long_strike: 185,
    short_strike: 150,
    width: 35,
    quantity: 5,
    limit_cap: 0.5,
    days_to_expiry: 24,
    days_to_deadline: 17,
  },
]

describe('MarksTable', () => {
  it('colors unrealized by sign and totals', () => {
    const marks: Marks = {
      ts: new Date().toISOString(),
      age_seconds: 1,
      total_unrealized: -5,
      spots: {},
      structures: {
        'nvda-oct': {
          qty: 5,
          entry: 0.21,
          bid: 0.19,
          ask: 0.21,
          mark: 0.2,
          unrealized: -5,
        },
      },
    }
    render(<MarksTable marks={marks} specs={specs} />)
    // row unrealized + total row both show -$5, both neg-colored
    const cells = screen.getAllByText('-$5')
    expect(cells).toHaveLength(2)
    for (const cell of cells) {
      const holder = cell.tagName === 'STRONG' ? cell.parentElement : cell
      expect(holder?.className).toContain('pnl-neg')
    }
    expect(screen.getByText('Total unrealized')).toBeTruthy()
  })

  it('renders a no-quote row when mark is null', () => {
    const marks: Marks = {
      ts: new Date().toISOString(),
      age_seconds: 1,
      total_unrealized: null,
      spots: {},
      structures: { 'nvda-oct': { qty: 4, entry: 1.37, bid: null, ask: null, mark: null, unrealized: null } },
    }
    render(<MarksTable marks={marks} specs={specs} />)
    expect(screen.getByText('no quote this cycle')).toBeTruthy()
  })

  it('states a multi-hour staleness in hours (M8 flash review: "33243s ago")', () => {
    const marks: Marks = {
      ts: new Date(Date.now() - 33243 * 1000).toISOString(),
      age_seconds: 33243,
      total_unrealized: -3,
      spots: {},
      structures: {
        'nvda-oct': { qty: 5, entry: 0.21, bid: 0.18, ask: 0.21, mark: 0.2, unrealized: -7.5 },
      },
    }
    render(<MarksTable marks={marks} specs={specs} />)
    expect(screen.getByText(/9h 14m ago — stale, monitor not refreshing/)).toBeTruthy()
    expect(screen.queryByText(/\d{4,}s ago/)).toBeNull()
  })

  it('shows the not-yet note without marks', () => {
    render(<MarksTable marks={null} specs={specs} />)
    expect(screen.getByText(/No marks yet/)).toBeTruthy()
  })
})
