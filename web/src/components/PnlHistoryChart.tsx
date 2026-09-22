// Total-book unrealized P&L over time. Thin wrapper over the shared
// TimeSeriesChart; kept as its own component so the plan page's aria
// label and any P&L-specific styling stay explicit.

import type { HistorySeries } from '../lib/types'
import { etTimeMs, usdSigned } from '../lib/format'
import { TimeSeriesChart } from './TimeSeriesChart'

export function PnlHistoryChart({ series }: { series: HistorySeries }) {
  const pts = series.points
  const t0 = pts[0][0]
  const t1 = pts[pts.length - 1][0]
  const last = pts[pts.length - 1][1]
  return (
    <TimeSeriesChart
      series={series}
      ariaLabel={`Total unrealized P&L from ${etTimeMs(t0)} to ${etTimeMs(t1)} ET, latest ${usdSigned(last)}`}
    />
  )
}
