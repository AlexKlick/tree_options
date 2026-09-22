import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { FillsTable } from './FillsTable'
import type { FillRow } from '../lib/fills'

const fills: FillRow[] = [
  { ts: '2026-09-22T13:00:09-04:00', side: 'exit', structureId: 'nvda-oct', qty: 2, price: 0.35, cash: 70 },
  { ts: '2026-09-22T11:33:12-04:00', side: 'entry', structureId: 'nvda-oct', qty: 5, price: 0.21, cash: -105 },
]

describe('FillsTable', () => {
  it('lists fills newest-first with side chips and signed cash', () => {
    render(<FillsTable fills={fills} />)
    const rows = screen.getAllByRole('row')
    expect(rows).toHaveLength(3) // header + 2 fills
    expect(rows[1].textContent).toContain('exit')
    expect(rows[2].textContent).toContain('entry')
    expect(screen.getByText('-$105')).toBeTruthy()
    expect(screen.getByText('+$70')).toBeTruthy()
    expect(screen.getByText('$0.21')).toBeTruthy()
    expect(screen.getByText('13:00')).toBeTruthy() // ET, 24h via Intl
  })

  it('shows the empty note without fills', () => {
    render(<FillsTable fills={[]} />)
    expect(screen.getByText(/No fills yet/)).toBeTruthy()
  })
})
