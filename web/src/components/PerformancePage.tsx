// Paper-money performance: equity curve (net liquidization - account
// truth), trading P&L by day, and per-plan/per-structure breakdowns.
// Two separately-labeled bases by design: equity may include non-trading
// activity; the P&L table is book-derived. Missing marks are gaps ("—"),
// never zeros.

import { getStats } from '../lib/api'
import { usd2, usdLevel, usdSigned } from '../lib/format'
import { usePoll } from '../hooks/usePoll'
import type { StatsResponse } from '../lib/types'
import { AppShell } from './AppShell'
import { Pill } from './Pill'
import { TimeSeriesChart } from './TimeSeriesChart'

const pnlClass = (v: number | null): string =>
  v === null ? '' : v >= 0 ? 'pnl-pos' : 'pnl-neg'

function StatTile({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="tile">
      <p>{label}</p>
      <div className={`num tile-value ${cls ?? ''}`}>{value}</div>
    </div>
  )
}

function TrackingPill({ since }: { since: string | null }) {
  if (!since) return null
  const day = since.slice(0, 10)
  return (
    <Pill variant="empty">○ tracking began {day}</Pill>
  )
}

export function PerformancePage() {
  const poll = usePoll(getStats)
  const d: StatsResponse | null = poll.data
  const totals = d?.totals
  return (
    <AppShell title="Performance" poll={poll}>
      {poll.data === null && poll.error ? (
        <div className="card empty-state">
          <p className="muted">Cannot reach the cockpit API. {poll.error}</p>
        </div>
      ) : (
        <>
          <div className="pill-row" style={{ marginBottom: 14 }}>
            <TrackingPill since={d?.tracking_since ?? null} />
            {d?.equity_account && <Pill variant="empty">account {d.equity_account}</Pill>}
            <Pill variant="empty">equity = net liquidization · P&L = book-derived</Pill>
          </div>

          <div className="grid tiles-grid">
            <StatTile label="Realized" value={usdSigned(totals?.realized ?? 0)} cls={pnlClass(totals?.realized ?? 0)} />
            <StatTile
              label="Unrealized (latest)"
              value={totals?.unrealized_last != null ? usdSigned(totals.unrealized_last) : '—'}
              cls={pnlClass(totals?.unrealized_last ?? null)}
            />
            <StatTile
              label="Win rate"
              value={
                totals?.win_rate != null
                  ? `${totals.wins}W / ${totals.losses}L · ${(totals.win_rate * 100).toFixed(0)}%`
                  : `${totals?.wins ?? 0}W / ${totals?.losses ?? 0}L`
              }
            />
            <StatTile label="Structures closed" value={String(totals?.structures_closed ?? 0)} />
          </div>

          <h2 className="section-title">
            Equity{' '}
            <span className="muted section-sub">(net liquidation · may include non-trading activity)</span>
          </h2>
          {d?.equity ? (
            <div className="card chart-card">
              <TimeSeriesChart
                series={d.equity}
                ariaLabel="Account net liquidation over time"
                valueFormat={usdLevel}
              />
            </div>
          ) : (
            <div className="card empty-state">
              <p className="muted">
                Equity tracking begins once the discovery loop records account
                samples (one per minute while the gateway is up).
              </p>
            </div>
          )}

          <h2 className="section-title">
            P&amp;L by day <span className="muted section-sub">(realized + end-of-day unrealized)</span>
          </h2>
          {d?.days.length ? (
            <div className="card table-card">
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th className="num">Realized</th>
                      <th className="num">Unrealized EOD</th>
                      <th className="num">Day P&L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.days.map((day) => (
                      <tr key={day.date}>
                        <td>{day.date}</td>
                        <td className={`num ${pnlClass(day.realized)}`}>
                          {usdSigned(day.realized)}
                        </td>
                        <td className={`num ${pnlClass(day.unrealized_eod)}`}>
                          {day.unrealized_eod != null ? usdSigned(day.unrealized_eod) : '—'}
                        </td>
                        <td className={`num ${pnlClass(day.total)}`}>
                          {day.total != null ? usdSigned(day.total) : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <div className="card empty-state">
              <p className="muted">No marks history yet — the first session day populates this table.</p>
            </div>
          )}

          <h2 className="section-title">Plans</h2>
          <div className="card table-card">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Plan</th>
                    <th className="num">Realized</th>
                    <th className="num">Unrealized (latest)</th>
                    <th className="num">Closed</th>
                  </tr>
                </thead>
                <tbody>
                  {(d?.per_plan ?? []).map((p) => (
                    <tr key={p.plan_id}>
                      <td>{p.plan_id}</td>
                      <td className={`num ${pnlClass(p.realized)}`}>{usdSigned(p.realized)}</td>
                      <td className={`num ${pnlClass(p.unrealized_last)}`}>
                        {p.unrealized_last != null ? usdSigned(p.unrealized_last) : '—'}
                      </td>
                      <td className="num">{p.structures_closed}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <h2 className="section-title">Structures</h2>
          <div className="card table-card">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Plan</th>
                    <th>Structure</th>
                    <th>Underlying</th>
                    <th>Status</th>
                    <th className="num">Entry</th>
                    <th className="num">Filled</th>
                    <th className="num">Realized</th>
                  </tr>
                </thead>
                <tbody>
                  {(d?.per_structure ?? []).map((s) => (
                    <tr key={`${s.plan_id}/${s.structure_id}`}>
                      <td className="muted">{s.plan_id}</td>
                      <td>
                        <code>{s.structure_id}</code>
                      </td>
                      <td>{s.underlying}</td>
                      <td>
                        <span className={`badge badge-${s.status}`}>{s.status}</span>
                      </td>
                      <td className="num">{s.entry_fill != null ? usd2(s.entry_fill) : '—'}</td>
                      <td className="num">{s.filled_qty}</td>
                      <td className={`num ${pnlClass(s.realized)}`}>
                        {s.realized != null ? usdSigned(s.realized) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </AppShell>
  )
}
