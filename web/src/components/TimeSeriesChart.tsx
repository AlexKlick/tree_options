// Generic (time, value) line chart: crosshair snaps to the nearest
// recorded sample; the last point is directly labeled. Drawn at its
// measured pixel width (ResizeObserver; 640 until measured), so labels
// stay 12px on a phone and the chart does not grow 700px tall on a
// widescreen. Shared by equity curves, stock charts, P&L history, and
// valuation scenarios.

import {
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from 'react'
import type { HistorySeries } from '../lib/types'
import { clamp, nearestIndex } from '../lib/interp'
import { chartGeometry, endLabel, tipTransform } from '../lib/chart'
import { etTimeMs, usdSigned } from '../lib/format'

const DEFAULT_WIDTH = 640

function useMeasuredWidth(ref: RefObject<HTMLDivElement | null>): number {
  const [width, setWidth] = useState(DEFAULT_WIDTH)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width
      if (w && w > 0) setWidth(w)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])
  return width
}

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
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const svgRef = useRef<SVGSVGElement | null>(null)
  const [hover, setHover] = useState<{ i: number; cssX: number; cssW: number } | null>(null)
  const width = useMeasuredWidth(wrapRef)

  const pts = series.points
  const t0 = pts[0][0]
  const t1Raw = pts[pts.length - 1][0]
  const t1 = t1Raw === t0 ? t0 + 1 : t1Raw
  const flat = series.y_lo === series.y_hi
  const yLo = flat ? series.y_lo - 1 : series.y_lo
  const yHi = flat ? series.y_hi + 1 : series.y_hi
  // zero gets a gridline only when the axis actually contains it (level
  // series such as equity or prices sit far from $0)
  const ticks = Array.from(new Set(yLo <= 0 && 0 <= yHi ? [yHi, 0, yLo] : [yHi, yLo]))

  const g = chartGeometry(width, ticks.map(valueFormat))
  const plotW = g.vw - g.padL - g.padR
  const plotH = g.vh - g.padT - g.padB
  const sx = (t: number): number => g.padL + ((t - t0) / (t1 - t0)) * plotW
  const sy = (v: number): number => g.padT + ((yHi - v) / (yHi - yLo)) * plotH
  const line = pts.map(([t, v]) => `${sx(t).toFixed(1)},${sy(v).toFixed(1)}`).join(' ')

  const ts = pts.map(([t]) => t)
  const lastPt = pts[pts.length - 1]
  const lastPos = lastPt[1] >= 0
  const lastText = valueFormat(lastPt[1])
  const lastAt = endLabel(sx(lastPt[0]), lastText, g.vw)

  const onPointerMove = (e: ReactPointerEvent<SVGRectElement>): void => {
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    if (rect.width === 0) return
    const vx = clamp(((e.clientX - rect.left) / rect.width) * g.vw, g.padL, g.vw - g.padR)
    const t = t0 + ((vx - g.padL) / plotW) * (t1 - t0)
    const i = nearestIndex(ts, t)
    setHover({ i, cssX: (sx(ts[i]) / g.vw) * rect.width, cssW: rect.width })
  }
  const onPointerLeave = (): void => setHover(null)

  const hoverPt = hover ? pts[hover.i] : null

  return (
    <div ref={wrapRef} className="chart-wrap" role="img" aria-label={ariaLabel}>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${g.vw} ${g.vh}`}
        className="chart-svg"
        aria-hidden="true"
      >
        {/* gridlines + y labels (deduped: an extent can sit exactly on 0) */}
        {ticks.map((v) => (
          <g key={v}>
            <line
              x1={g.padL}
              x2={g.vw - g.padR}
              y1={sy(v)}
              y2={sy(v)}
              className={v === 0 ? 'zero-line' : 'gridline'}
            />
            <text
              x={g.padL - 8}
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
          x={lastAt.x}
          y={sy(lastPt[1])}
          textAnchor={lastAt.anchor}
          dominantBaseline="middle"
          className={`plateau-label ${lastPos ? 'fill-pos' : 'fill-neg'}`}
        >
          {lastText}
        </text>

        {/* time axis: first + last sample */}
        <text x={g.padL} y={g.vh - g.padB + 18} className="axis-label">
          {timeFormat(t0)}
        </text>
        <text x={g.vw - g.padR} y={g.vh - g.padB + 18} textAnchor="end" className="axis-label">
          {timeFormat(t1Raw)}
        </text>

        {/* crosshair snapped to the nearest sample */}
        {hoverPt && (
          <g>
            <line
              x1={sx(hoverPt[0])}
              x2={sx(hoverPt[0])}
              y1={g.padT}
              y2={g.vh - g.padB}
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
          x={g.padL}
          y={g.padT}
          width={plotW}
          height={plotH + g.padB}
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
