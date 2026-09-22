// Execution record derived from the append-only events ledger: every
// entry/exit fill with its signed cash flow. Presentational projection —
// the authoritative realized P&L stays server-computed per structure.

import type { EventRecord } from './types'

export interface FillRow {
  ts: string
  side: 'entry' | 'exit'
  structureId: string
  qty: number
  price: number
  /** dollars moved: entries pay (negative), exits collect (positive) */
  cash: number
}

const MULT = 100

export function deriveFills(events: EventRecord[]): FillRow[] {
  const rows: FillRow[] = []
  for (const e of events) {
    const kind = typeof e.event === 'string' ? e.event : ''
    if (kind !== 'entry_fill' && kind !== 'exit_fill') continue
    const structureId = typeof e.structure === 'string' ? e.structure : ''
    const qty = Number(e.filled)
    const price = Number(e.avg)
    if (!structureId || !Number.isFinite(qty) || !Number.isFinite(price)) continue
    const side = kind === 'entry_fill' ? 'entry' : 'exit'
    rows.push({
      ts: typeof e.ts === 'string' ? e.ts : '',
      side,
      structureId,
      qty,
      price,
      cash: (side === 'entry' ? -1 : 1) * qty * price * MULT,
    })
  }
  return rows.reverse()
}
