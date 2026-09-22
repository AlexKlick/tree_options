import type { FillRow } from '../lib/fills'
import { etTime, usd2, usdSigned } from '../lib/format'

/** Execution record: every fill with its signed cash flow (newest first). */
export function FillsTable({ fills }: { fills: FillRow[] }) {
  if (fills.length === 0) {
    return (
      <div className="card">
        <p className="muted" style={{ margin: 0 }}>
          No fills yet.
        </p>
      </div>
    )
  }
  return (
    <div className="card table-card">
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Time (ET)</th>
              <th>Side</th>
              <th>Structure</th>
              <th className="num">Qty</th>
              <th className="num">Price</th>
              <th className="num">Cash flow</th>
            </tr>
          </thead>
          <tbody>
            {fills.map((f, i) => (
              <tr key={`${f.ts}-${f.structureId}-${i}`}>
                <td className="num">{etTime(f.ts)}</td>
                <td>
                  <span className={`badge badge-${f.side}`}>{f.side}</span>
                </td>
                <td>
                  <code>{f.structureId}</code>
                </td>
                <td className="num">{f.qty}</td>
                <td className="num">{usd2(f.price)}</td>
                <td className={`num ${f.cash >= 0 ? 'pnl-pos' : 'pnl-neg'}`}>
                  {usdSigned(f.cash)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
