import type { NetPosition } from '../lib/types'
import { usd, usd2, usdSigned } from '../lib/format'
import { TableScroll } from './TableScroll'

const pnlClass = (v: number | null): string =>
  v === null ? '' : v >= 0 ? 'pnl-pos' : 'pnl-neg'

/** Net exposure per underlying: the book the way risk aggregates. */
export function NetPositionsTable({ rows }: { rows: NetPosition[] }) {
  if (rows.length === 0) return null
  return (
    <div className="card table-card">
      <TableScroll>
        <table>
          <thead>
            <tr>
              <th>Underlying</th>
              <th className="num">Open</th>
              <th className="num">Avg entry</th>
              <th className="num">Committed</th>
              <th className="num">Max gain</th>
              <th className="num">Unrealized</th>
              <th className="num">Wings</th>
              <th>Legs</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.underlying}>
                <td>
                  <strong>{r.underlying}</strong>
                  <span className="muted leg-note">
                    {' '}
                    {r.structure_count} struct{r.structure_count === 1 ? '' : 's'}
                  </span>
                </td>
                <td className="num strike">{r.open_qty}</td>
                <td className="num">{r.avg_entry !== null ? usd2(r.avg_entry) : '—'}</td>
                <td className="num">
                  {r.committed !== null ? (
                    usd(r.committed)
                  ) : (
                    <span className="muted" title="some fills have no price yet — the full cost is unknown">
                      unknown
                      {r.committed_known > 0 ? ` (${usd(r.committed_known)} known)` : ''}
                    </span>
                  )}
                </td>
                <td className={`num ${r.max_gain !== null ? 'pnl-pos' : 'muted'}`}>
                  {r.max_gain !== null ? usdSigned(r.max_gain) : '—'}
                </td>
                <td className={`num ${pnlClass(r.unrealized)}`}>
                  {r.unrealized !== null ? usdSigned(r.unrealized) : '—'}
                </td>
                <td className="num strike">
                  {r.short_floor} / {r.long_ceiling}
                </td>
                <td>
                  <div className="leg-chips">
                    {r.legs.map((leg) => (
                      <span className="leg-chip" key={leg.structure_id}>
                        <code>{leg.structure_id}</code> {leg.long_strike}/{leg.short_strike}{' '}
                        &times;{leg.open_qty}
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
    </div>
  )
}
