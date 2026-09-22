// Alternatives: forward-shadow paper positions for candidates NOT taken.
// Every row carries the "forward-shadow · not executed" label — these are
// observation positions opened at scan-time mids, never orders.

import type { ShadowBlock } from '../lib/types'
import { usd2, usdSigned } from '../lib/format'
import { Pill } from './Pill'

const pnlClass = (v: number | null): string =>
  v === null ? '' : v >= 0 ? 'pnl-pos' : 'pnl-neg'

const MARK_SOURCE_LABEL: Record<string, string> = {
  scan: 'scan row',
  chain: 'chain',
  carry: 'carried (no re-quote)',
  'intrinsic-approx': 'intrinsic approx',
  none: 'no mark yet',
}

export function ShadowSection({ shadow }: { shadow: ShadowBlock | null }) {
  if (!shadow || shadow.positions.length === 0) return null
  const open = shadow.positions.filter((p) => p.status === 'open')
  const expired = shadow.positions.filter((p) => p.status !== 'open')
  return (
    <>
      <h2 className="section-title">
        Alternatives{' '}
        <span className="muted section-sub">
          (forward-shadow · not executed · marked at each scan)
        </span>{' '}
        <Pill variant="empty">paper observation only</Pill>
      </h2>
      <div className="card table-card">
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Underlying</th>
                <th>Expiry</th>
                <th>Wings</th>
                <th className="num">Debit paid</th>
                <th className="num">Last mark</th>
                <th>Mark source</th>
                <th className="num">P&amp;L</th>
                <th className="num">Best</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {[...open, ...expired].map((p) => (
                <tr key={p.episode_id}>
                  <td>
                    <strong>{p.underlying}</strong>
                  </td>
                  <td className="muted">{p.expiry}</td>
                  <td className="num strike">
                    {p.long_strike}/{p.short_strike}
                  </td>
                  <td className="num">{usd2(p.debit_paid)}</td>
                  <td className="num">
                    {p.last_mark != null ? usd2(p.last_mark) : '—'}
                  </td>
                  <td className="muted">{MARK_SOURCE_LABEL[p.mark_source] ?? p.mark_source}</td>
                  <td className={`num ${pnlClass(p.pnl)}`}>
                    {p.pnl != null ? usdSigned(p.pnl) : '—'}
                  </td>
                  <td className={`num ${pnlClass(p.best_pnl)}`}>
                    {p.best_pnl != null ? usdSigned(p.best_pnl) : '—'}
                  </td>
                  <td>
                    <span className={`badge badge-${p.status === 'open' ? 'open' : 'closed'}`}>
                      {p.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          {shadow.stats.open} open · {shadow.stats.expired} expired
          {shadow.stats.mean_pnl != null &&
            ` · mean ${usdSigned(shadow.stats.mean_pnl)}`}
          {shadow.stats.hit_rate != null &&
            ` · hit ${Math.round(shadow.stats.hit_rate * 100)}%`}
          {' '}· opened from scans, never executed at a broker
        </p>
      </div>
    </>
  )
}
