import { useState } from 'react'
import { etTime } from '../lib/format'
import type { EventRecord } from '../lib/types'

function summary(e: EventRecord): string {
  const parts: string[] = []
  for (const key of ['structure', 'qty', 'filled', 'avg', 'limit', 'order', 'reason', 'status', 'cycle', 'detail']) {
    const v = e[key]
    if (v !== undefined && v !== null && String(v).length > 0) {
      parts.push(`${key}=${String(v)}`)
    }
  }
  return parts.join(' · ')
}

/** The execution ledger: every order/fill/reprice/abort, filterable. */
export function EventsLedger({ events }: { events: EventRecord[] }) {
  const [kind, setKind] = useState<string | null>(null)
  const [structure, setStructure] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<number | null>(null)

  if (events.length === 0) {
    return (
      <div className="card">
        <p className="muted" style={{ margin: 0 }}>
          No events recorded yet for this run.
        </p>
      </div>
    )
  }

  const kinds = Array.from(new Set(events.map((e) => String(e.event ?? '?'))))
  const structures = Array.from(
    new Set(
      events
        .map((e) => e.structure)
        .filter((v): v is string => typeof v === 'string'),
    ),
  )
  const filtered = [...events]
    .reverse()
    .filter(
      (e) =>
        (!kind || e.event === kind) &&
        (!structure || e.structure === structure),
    )

  return (
    <div className="card">
      <div className="chip-row" role="group" aria-label="Filter events">
        <button
          type="button"
          className="chip"
          aria-pressed={kind === null}
          onClick={() => setKind(null)}
        >
          all kinds
        </button>
        {kinds.map((k) => (
          <button
            key={k}
            type="button"
            className="chip"
            aria-pressed={kind === k}
            onClick={() => setKind(kind === k ? null : k)}
          >
            {k}
          </button>
        ))}
        {structures.length > 1 &&
          structures.map((s) => (
            <button
              key={s}
              type="button"
              className="chip chip-alt"
              aria-pressed={structure === s}
              onClick={() => setStructure(structure === s ? null : s)}
            >
              {s}
            </button>
          ))}
      </div>
      <p className="muted table-note">
        {filtered.length} of {events.length} events, newest first — click a row
        for the raw record.
      </p>
      <div className="ledger">
        {filtered.map((e, i) => {
          const isOpen = expanded === i
          return (
            <div key={i} className="ledger-row">
              <button
                type="button"
                className="ledger-head"
                aria-expanded={isOpen}
                onClick={() => setExpanded(isOpen ? null : i)}
              >
                <span className="muted num">{e.ts ? etTime(String(e.ts)) : '?'}</span>
                <code>{String(e.event ?? '?')}</code>
                <span className="muted ledger-summary">{summary(e)}</span>
              </button>
              {isOpen && (
                <pre>{JSON.stringify(e, null, 2)}</pre>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
