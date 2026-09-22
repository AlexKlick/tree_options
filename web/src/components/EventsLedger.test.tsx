import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { EventsLedger } from './EventsLedger'
import type { EventRecord } from '../lib/types'

const events: EventRecord[] = [
  { ts: '2026-09-22T15:25:16-04:00', event: 'entry_order', structure: 'nvda-oct', limit: '0.21' },
  { ts: '2026-09-22T15:33:12-04:00', event: 'entry_fill', structure: 'nvda-oct', filled: 5, avg: '0.21' },
  { ts: '2026-09-22T15:36:04-04:00', event: 'entry_order', structure: 'nvda-nov', limit: '1.22' },
]

describe('EventsLedger', () => {
  it('filters by event-kind chip and expands a row', () => {
    render(<EventsLedger events={events} />)
    // the entry_order chip + the two entry_order ledger rows
    expect(screen.getAllByRole('button', { name: /entry_order/ })).toHaveLength(3)

    fireEvent.click(screen.getByRole('button', { name: 'entry_fill' }))
    // the chip + exactly one ledger row (the filter matched one event)
    expect(screen.getAllByRole('button', { name: /entry_fill/ })).toHaveLength(2)

    fireEvent.click(screen.getByText(/filled=5/))
    expect(screen.getByText(/"avg": "0.21"/)).toBeTruthy()
  })

  it('shows the empty note without events', () => {
    render(<EventsLedger events={[]} />)
    expect(screen.getByText(/No events recorded yet/)).toBeTruthy()
  })
})
