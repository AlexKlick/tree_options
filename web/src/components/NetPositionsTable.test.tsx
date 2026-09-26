import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { NetPositionsTable } from './NetPositionsTable'
import type { NetPosition } from '../lib/types'
import { usdSigned } from '../lib/format'

const nvda: NetPosition = {
  underlying: 'NVDA',
  structure_count: 2,
  open_qty: 8,
  avg_entry: 0.64125,
  committed: 477,
  committed_known: 477,
  cost_unknown: false,
  unpriced_qty: 0,
  max_gain: 27523,
  max_loss: -477,
  unrealized: -11,
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
    {
      structure_id: 'nvda-nov',
      expiry: '2026-11-20',
      long_strike: 185,
      short_strike: 150,
      open_qty: 3,
      entry: 1.24,
      entry_unpriced_qty: 0,
    },
  ],
}

describe('NetPositionsTable', () => {
  it('renders one row per underlying with the summed exposure', () => {
    render(<NetPositionsTable rows={[nvda]} />)
    expect(screen.getByText('NVDA')).toBeTruthy()
    expect(screen.getByText('8')).toBeTruthy()
    expect(screen.getByText('$477')).toBeTruthy()
    expect(screen.getByText(usdSigned(27523))).toBeTruthy()
    expect(screen.getByText(usdSigned(-11))).toBeTruthy()
    // wings cell: short floor / long ceiling
    expect(screen.getByText('150 / 185')).toBeTruthy()
    // each leg listed as a chip with id, wings, qty
    expect(screen.getByText(/nvda-oct/)).toBeTruthy()
    expect(screen.getByText(/nvda-nov/)).toBeTruthy()
    expect(screen.getByText(/×5/)).toBeTruthy()
    expect(screen.getByText(/×3/)).toBeTruthy()
  })

  it('discloses unknown cost with the known subtotal (R3-02)', () => {
    render(
      <NetPositionsTable
        rows={[
          {
            ...nvda,
            committed: null,
            avg_entry: null,
            max_gain: null,
            max_loss: null,
            cost_unknown: true,
            unpriced_qty: 2,
            committed_known: 377,
          },
        ]}
      />,
    )
    expect(screen.getByText(/unknown/)).toBeTruthy()
    expect(screen.getByText(/\$377 known/)).toBeTruthy()
  })

  it('renders an em-dash when no quote feeds the unrealized cell', () => {
    render(<NetPositionsTable rows={[{ ...nvda, unrealized: null }]} />)
    expect(screen.getByText('—')).toBeTruthy()
  })

  it('renders nothing when no structure is open', () => {
    const { container } = render(<NetPositionsTable rows={[]} />)
    expect(container.querySelector('table')).toBeNull()
  })
})
