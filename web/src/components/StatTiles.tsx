import { useTween } from '../hooks/useTween'
import { usd, usdSigned } from '../lib/format'
import type { BookSummary, Marks } from '../lib/types'

function Tile({
  label,
  value,
  className,
}: {
  label: string
  value: string
  className?: string
}) {
  return (
    <div className="card tile">
      <p className="tile-label">{label}</p>
      <div className={`tile-value num ${className ?? ''}`}>{value}</div>
    </div>
  )
}

/** Book-level stat tiles; "unrealized now" tweens between polls. */
export function StatTiles({
  summary,
  marks,
}: {
  summary: BookSummary | null
  marks: Marks | null
}) {
  const total = marks?.total_unrealized ?? null
  const tweened = useTween(total ?? 0)
  if (!summary) return null
  return (
    <div className="tiles">
      <Tile label="Committed debit" value={usd(summary.committed)} />
      {total === null ? (
        <Tile label="Unrealized now" value="—" />
      ) : (
        <Tile
          label="Unrealized now"
          value={usdSigned(tweened)}
          className={total >= 0 ? 'pnl-pos' : 'pnl-neg'}
        />
      )}
      <Tile
        label={
          summary.short_floor !== null
            ? `Max profit (≤ ${summary.short_floor})`
            : 'Max profit'
        }
        value={usdSigned(summary.max_gain)}
        className="pnl-pos"
      />
      <Tile
        label="Max loss (≥ longs)"
        value={usdSigned(summary.max_loss)}
        className="pnl-neg"
      />
    </div>
  )
}
