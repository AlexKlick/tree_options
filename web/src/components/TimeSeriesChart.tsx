// Generic (time, value) line chart: crosshair snaps to the nearest
// recorded sample; the last point is directly labeled. Fixed-viewBox SVG,
// width 100% (scales with the shell, no JS resize). Extracted from
// PnlHistoryChart so equity curves, stock charts, and valuation scenarios
// share one implementation.

import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { HistorySeries } from '../lib/types'
import { clamp, nearestIndex } from '../lib/interp'
import { tipTransform } from '../lib/chart'
import { etTimeMs, usdSigned } from '../lib/format'

const VW = 640
const VH = 200
const PAD = { l: 64, r: 64, t: 16, b: 26 }

export interface TimeSeriesChartProps {
  series: HistorySeries
  ariaLabel: string
  valueFormat?: (v: number) => string
  timeFormat?: (ts: number) => string
}

export function TimeSeriesChart({
  series,
  ariaLabel,
  valueFormat = usdSigned,
  timeFormat = (ts: number) => `${etTimeMs(ts)} ET`,
}: TimeSeriesChartProps) {
  const svgRef = useRef<SVGSVGElement | null>(null)
  const [hover, setHover] = useState<{ i: number; cssX: number; cssW: number } | null>(null)

  const pts = series.points
  const t0 = pts[0][0]
  const t1Raw = pts[pts.length - 1][0]
  const t1 = t1Raw === t0 ? t0 + 1 : t1Raw
  const flat = series.y_lo === series.y_hi
  const yLo = flat ? series.y_lo - 1 : series.y_lo
  const yHi = flat ? series.y_hi + 1 : series.y_hi

  const plotW = VW - PAD.l - PAD.r
  const plotH = VH - PAD.t - PAD.b
  const sx = (t: number): number => PAD.l + ((t - t0) / (t1 - t0)) * plotW
  const sy = (v: number): number => PAD.t + ((yHi - v) / (yHi - yLo)) * plotH
  const line = pts.map(([t, v]) => `${sx(t).toFixed(1)},${sy(v).toFixed(1)}`).join(' ')

  const ts = pts.map(([t]) => t)
  const lastPt = pts[pts.length - 1]
  const lastPos = lastPt[1] >= 0

  const onPointerMove = (e: ReactPointerEvent<SVGRectElement>): void => {
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    if (rect.width === 0) return
    const vx = clamp(((e.clientX - rect.left) / rect.width) * VW, PAD.l, VW - PAD.r)
    const t = t0 + ((vx - PAD.l) / plotW) * (t1 - t0)
    const i = nearestIndex(ts, t)
    setHover({ i, cssX: (sx(ts[i]) / VW) * rect.width, cssW: rect.width })
  }
  const onPointerLeave = (): void => setHover(null)

  const hoverPt = hover ? pts[hover.i] : null

  return (
    <div className="chart-wrap" role="img" aria-label={ariaLabel}>
      <svg ref={svgRef} viewBox={`0 0 ${VW} ${VH}`} className="chart-svg" aria-hidden="true">
        {/* gridlines + y labels (deduped: an extent can sit exactly on 0) */}
        {Array.from(new Set([yHi, 0, yLo])).map((v) => (
          <g key={v}>
            <line
              x1={PAD.l}
              x2={VW - PAD.r}
              y1={sy(v)}
              y2={sy(v)}
              className={v === 0 ? 'zero-line' : 'gridline'}
            />
            <text
              x={PAD.l - 8}
              y={sy(v)}
              textAnchor="end"
              dominantBaseline="middle"
              className="axis-label"
            >
              {valueFormat(v)}
            </text>
          </g>
        ))}

        {/* the value line + directly labeled last point */}
        <polyline points={line} className="payoff-line" />
        <circle
          cx={sx(lastPt[0])}
          cy={sy(lastPt[1])}
          r={4}
          className={`dot-ring ${lastPos ? 'fill-pos' : 'fill-neg'}`}
        />
        <text
          x={sx(lastPt[0]) + 8}
          y={sy(lastPt[1])}
          dominantBaseline="middle"
          className={`plateau-label ${lastPos ? 'fill-pos' : 'fill-neg'}`}
        >
          {valueFormat(lastPt[1])}
        </text>

        {/* time axis: first + last sample */}
        <text x={PAD.l} y={VH - PAD.b + 18} className="axis-label">
          {timeFormat(t0)}
        </text>
        <text x={VW - PAD.r} y={VH - PAD.b + 18} textAnchor="end" className="axis-label">
          {timeFormat(t1Raw)}
        </text>

        {/* crosshair snapped to the nearest sample */}
        {hoverPt && (
          <g>
            <line
              x1={sx(hoverPt[0])}
              x2={sx(hoverPt[0])}
              y1={PAD.t}
              y2={VH - PAD.b}
              className="crosshair"
            />
            <circle
              cx={sx(hoverPt[0])}
              cy={sy(hoverPt[1])}
              r={4}
              className="fill-ink dot-ring"
            />
          </g>
        )}

        <rect
          x={PAD.l}
          y={PAD.t}
          width={plotW}
          height={plotH + PAD.b}
          fill="transparent"
          style={{ touchAction: 'none' }}
          onPointerMove={onPointerMove}
          onPointerLeave={onPointerLeave}
        />
      </svg>

      {hoverPt && hover && (
        <div
          className="chart-tip"
          style={{
            left: hover.cssX,
            transform: tipTransform(hover.cssX, hover.cssW, 0.6),
          }}
        >
          <div className="tip-price num">{timeFormat(hoverPt[0])}</div>
          <div className={`num ${hoverPt[1] >= 0 ? 'pnl-pos' : 'pnl-neg'}`}>
            {valueFormat(hoverPt[1])}
          </div>
        </div>
      )}
    </div>
  )
}
