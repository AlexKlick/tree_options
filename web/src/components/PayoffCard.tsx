import type { MarkRow, Payoff } from '../lib/types'
import { PayoffChart } from './PayoffChart'
import { usd2, usdSigned } from '../lib/format'

/** One structure's payoff card: chart + levels + the live mark when the
 * monitor has a quote for it. */
export function PayoffCard({
  payoff,
  mark,
}: {
  payoff: Payoff
  mark?: MarkRow | null
}) {
  const l = payoff.levels
  return (
    <div className="card chart-card">
      <div className="card-head">
        <h3>
          {payoff.underlying} {l.long_strike}/{l.short_strike} put spread
        </h3>
        <span className="muted num">
          {mark?.unrealized != null && (
            <strong className={mark.unrealized >= 0 ? 'pnl-pos' : 'pnl-neg'}>
              {usdSigned(mark.unrealized)}{' '}
            </strong>
          )}
          {l.qty}&times; @ {usd2(l.entry)}
        </span>
      </div>
      <PayoffChart payoff={payoff} />
      <dl className="kv">
        <dt>Breakeven</dt>
        <dd className="num strike">{usd2(l.breakeven)}</dd>
        <dt>Max gain</dt>
        <dd className="num pnl-pos">{payoff.labels.max_gain}</dd>
        <dt>Max loss</dt>
        <dd className="num pnl-neg">{payoff.labels.max_loss}</dd>
      </dl>
      <p className="chart-hint muted">
        Click or drag the chart to pin a price; arrow keys nudge by $0.50 (Shift
        $5); Esc clears.
      </p>
    </div>
  )
}
