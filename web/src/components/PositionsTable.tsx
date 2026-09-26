import { usd2, usdSigned } from '../lib/format'
import type { PlanStructureSpec, StructureStateView } from '../lib/types'
import { TableScroll } from './TableScroll'

const pnlClass = (v: number | null): string =>
  v === null ? '' : v >= 0 ? 'pnl-pos' : 'pnl-neg'

/** Execution state per structure: fills, open qty, realized P&L. */
export function PositionsTable({
  specs,
  structures,
}: {
  specs: PlanStructureSpec[]
  structures: Record<string, StructureStateView>
}) {
  return (
    <div className="card table-card">
      <TableScroll>
        <table>
          <thead>
            <tr>
              <th>Structure</th>
              <th>State</th>
              <th className="num">Strike</th>
              <th className="num">Entry</th>
              <th className="num">Filled</th>
              <th className="num">Open</th>
              <th className="num">Exit</th>
              <th className="num">Realized</th>
              <th className="num">Expiry</th>
            </tr>
          </thead>
          <tbody>
            {specs.map((s) => {
              const st = structures[s.id]
              if (!st) return null
              return (
                <tr key={s.id}>
                  <td>
                    <code>{s.id}</code>
                  </td>
                  <td>
                    <span className={`badge badge-${st.state}`}>{st.state}</span>
                  </td>
                  <td className="num strike">
                    {s.long_strike}/{s.short_strike}
                  </td>
                  <td className="num">
                    {st.entry_fill !== null ? usd2(st.entry_fill) : '—'}
                    {st.entry_unpriced_qty > 0 && (
                      <span
                        className="muted leg-note"
                        title={`${st.entry_unpriced_qty} fill(s) without a price: the average covers the priced fills only`}
                      >
                        {' '}
                        +{st.entry_unpriced_qty} unpriced
                      </span>
                    )}
                  </td>
                  <td className="num">
                    {st.filled_qty}/{s.quantity}
                  </td>
                  <td className="num">{st.open_qty}</td>
                  <td className="num">
                    {st.exit_fill !== null ? usd2(st.exit_fill) : '—'}
                    {st.exit_unpriced_qty > 0 && (
                      <span
                        className="muted leg-note"
                        title={`${st.exit_unpriced_qty} exit fill(s) without a price`}
                      >
                        {' '}
                        +{st.exit_unpriced_qty} unpriced
                      </span>
                    )}
                  </td>
                  <td className={`num ${pnlClass(st.realized_pnl)}`}>
                    {st.realized_pnl !== null ? (
                      usdSigned(st.realized_pnl)
                    ) : st.entry_unpriced_qty > 0 || st.exit_unpriced_qty > 0 ? (
                      <span className="muted" title="some fills have no price yet">
                        unknown
                      </span>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td className="num">
                    {s.expiry} ({s.days_to_expiry}d)
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </TableScroll>
    </div>
  )
}
