import { usd2, usdSigned } from '../lib/format'
import type { PlanStructureSpec, StructureStateView } from '../lib/types'

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
      <div style={{ overflowX: 'auto' }}>
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
                  </td>
                  <td className="num">
                    {st.filled_qty}/{s.quantity}
                  </td>
                  <td className="num">{st.open_qty}</td>
                  <td className="num">
                    {st.exit_fill !== null ? usd2(st.exit_fill) : '—'}
                  </td>
                  <td className={`num ${pnlClass(st.realized_pnl)}`}>
                    {st.realized_pnl !== null ? usdSigned(st.realized_pnl) : '—'}
                  </td>
                  <td className="num">
                    {s.expiry} ({s.days_to_expiry}d)
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
