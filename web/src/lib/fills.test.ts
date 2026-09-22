import { describe, expect, it } from 'vitest'
import { deriveFills } from './fills'
import type { EventRecord } from './types'

const events: EventRecord[] = [
  { ts: '2026-09-22T11:33:00-04:00', event: 'entry_order', structure: 'nvda-oct', limit: '0.21' },
  { ts: '2026-09-22T11:33:12-04:00', event: 'entry_fill', structure: 'nvda-oct', filled: 5, avg: '0.21' },
  { ts: '2026-09-22T11:35:40-04:00', event: 'entry_fill', structure: 'nvda-nov', filled: 3, avg: '1.24' },
  { ts: '2026-09-22T13:00:00-04:00', event: 'exit_order', structure: 'nvda-oct', limit: '0.35' },
  { ts: '2026-09-22T13:00:09-04:00', event: 'exit_fill', structure: 'nvda-oct', filled: 2, avg: '0.35' },
  { ts: '2026-09-22T13:00:10-04:00', event: 'book_repair', note: 'reloaded from disk' },
  { ts: '2026-09-22T12:00:00-04:00', event: 'entry_abort', structure: 'qqq-nov', reason: 'window closed' },
]

describe('deriveFills', () => {
  it('extracts entry+exit fills newest-first with signed cash flow', () => {
    const rows = deriveFills(events)
    expect(rows.map((r) => [r.side, r.structureId, r.qty, r.price])).toEqual([
      ['exit', 'nvda-oct', 2, 0.35],
      ['entry', 'nvda-nov', 3, 1.24],
      ['entry', 'nvda-oct', 5, 0.21],
    ])
    // exit brings cash in, entries pay
    expect(rows[0].cash).toBeCloseTo(2 * 100 * 0.35)
    expect(rows[1].cash).toBeCloseTo(-3 * 100 * 1.24)
    expect(rows[2].cash).toBeCloseTo(-5 * 100 * 0.21)
    expect(rows[0].ts).toBe('2026-09-22T13:00:09-04:00')
  })

  it('skips junk and malformed fill records', () => {
    expect(deriveFills([{ event: 'entry_fill' }, { foo: 1 }])).toEqual([])
    expect(
      deriveFills([{ event: 'entry_fill', structure: 'x', filled: 1, avg: 'oops' }]),
    ).toEqual([])
    expect(deriveFills([{ event: 'entry_fill', structure: 'x', filled: 'no', avg: '0.2' }])).toEqual(
      [],
    )
  })

  it('returns an empty list for a fill-less ledger', () => {
    expect(deriveFills(events.filter((e) => !String(e.event).endsWith('_fill')))).toEqual([])
  })
})
