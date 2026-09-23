import { ageSeconds, ago, etDateTime, usd2, usdSigned } from '../lib/format'
import type { Marks, PlanStructureSpec } from '../lib/types'
import { TableScroll } from './TableScroll'

const pnlClass = (v: number | null): string =>
  v === null ? '' : v >= 0 ? 'pnl-pos' : 'pnl-neg'

/** Live marks (delayed, mark-to-mid) with the total row and staleness note. */
export function MarksTable({
  marks,
  specs,
}: {
  marks: Marks | null
  specs: PlanStructureSpec[]
}) {
  if (!marks || Object.keys(marks.structures).length === 0) {
    return (
      <div className="card">
        <p className="muted" style={{ margin: 0 }}>
          No marks yet — the monitor writes marks.json every cycle (~20s) once
          a structure has filled.
        </p>
      </div>
    )
  }
  const age = ageSeconds(marks.ts)
  const rows = specs.filter((s) => marks.structures[s.id] !== undefined)
  return (
    <div className="card table-card">
      <TableScroll>
        <table>
          <thead>
            <tr>
              <th>Structure</th>
              <th className="num">Qty</th>
              <th className="num">Entry</th>
              <th className="num">Bid</th>
              <th className="num">Ask</th>
              <th className="num">Mark</th>
              <th className="num">Unrealized</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => {
              const m = marks.structures[s.id]
              return (
                <tr key={s.id}>
                  <td>
                    <code>{s.id}</code>
                  </td>
                  <td className="num">{m.qty ?? '—'}</td>
                  <td className="num">{m.entry !== null ? usd2(m.entry) : '—'}</td>
                  {m.mark !== null ? (
                    <>
                      <td className="num">{m.bid !== null ? usd2(m.bid) : '—'}</td>
                      <td className="num">{m.ask !== null ? usd2(m.ask) : '—'}</td>
                      <td className="num">
                        <strong>{usd2(m.mark)}</strong>
                      </td>
                      <td className={`num ${pnlClass(m.unrealized)}`}>
                        {m.unrealized !== null ? usdSigned(m.unrealized) : '—'}
                      </td>
                    </>
                  ) : (
                    <td className="muted" colSpan={4}>
                      no quote this cycle
                    </td>
                  )}
                </tr>
              )
            })}
            <tr className="total-row">
              <td colSpan={6}>
                <strong>Total unrealized</strong>
              </td>
              <td className={`num ${pnlClass(marks.total_unrealized)}`}>
                <strong>
                  {marks.total_unrealized !== null
                    ? usdSigned(marks.total_unrealized)
                    : '—'}
                </strong>
              </td>
            </tr>
          </tbody>
        </table>
      </TableScroll>
      <p className="muted table-note">
        as of <code>{marks.ts ? etDateTime(marks.ts) : '?'}</code>
        {age !== null
          ? ` · ${ago(age)} ago${age > 120 ? ' — stale, monitor not refreshing' : ''}`
          : ''}{' '}
        · delayed data, mark-to-mid
      </p>
    </div>
  )
}
