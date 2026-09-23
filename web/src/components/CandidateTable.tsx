import type { CandidateRow } from '../lib/types'
import { num2, usdSigned } from '../lib/format'
import { TableScroll } from './TableScroll'

const ratio = (v: number | null): string => (v === null ? '—' : `${v.toFixed(1)}:1`)

function Rules({ c }: { c: CandidateRow }) {
  if (c.rules.length === 0) return null
  return (
    <details>
      <summary className="muted">{c.reasons.length ? 'why rejected' : 'rule audit'}</summary>
      <ul className="rule-list">
        {c.rules.map((r, i) => (
          <li key={i}>
            <span className={`rule-status rule-${r.status.toLowerCase()}`}>{r.status}</span>{' '}
            {r.rule} — {r.detail}
          </li>
        ))}
      </ul>
    </details>
  )
}

/** Ranked candidate table (operator) — the tri-state rule audit rides
 * inside a collapsible per row so the main grid stays scannable. */
export function CandidateTable({
  rows,
  onScenario,
}: {
  rows: CandidateRow[]
  onScenario?: (c: CandidateRow) => void
}) {
  if (rows.length === 0) return null
  return (
    <div className="card table-card">
      <TableScroll>
        <table>
          <thead>
            <tr>
              <th className="num">#</th>
              <th>Underlying</th>
              <th className="num">Strikes</th>
              <th className="num">DTE</th>
              <th className="num">Debit</th>
              <th className="num">Max profit</th>
              <th className="num">Max loss</th>
              <th className="num">Yield</th>
              <th>Rules</th>
              {onScenario && <th>Scenario</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((c, i) => (
              <tr key={`${c.underlying}-${c.short_strike}-${c.width}-${i}`}>
                <td className="num muted">{c.accepted ? c.rank : '—'}</td>
                <td>
                  <strong>{c.underlying}</strong>
                  <span className="muted"> {c.expiry}</span>
                </td>
                <td className="num strike">
                  {c.long_strike}/{c.short_strike}
                </td>
                <td className="num">{c.dte}</td>
                <td className="num">{c.debit_mid !== null ? `$${num2(c.debit_mid)}` : '—'}</td>
                <td className="num pnl-pos">
                  {c.max_profit !== null ? usdSigned(c.max_profit) : '—'}
                </td>
                <td className="num pnl-neg">
                  {c.max_loss !== null ? usdSigned(c.max_loss) : '—'}
                </td>
                <td className="num strike">{ratio(c.yield_ratio)}</td>
                <td>
                  <Rules c={c} />
                </td>
                {onScenario && (
                  <td>
                    <button
                      type="button"
                      className="chip"
                      onClick={() => onScenario(c)}
                      aria-label={`Valuation scenario for ${c.underlying} ${c.long_strike}/${c.short_strike}`}
                    >
                      scenario
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </TableScroll>
    </div>
  )
}
