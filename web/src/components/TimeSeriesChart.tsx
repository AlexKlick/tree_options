// Generic (time, value) line chart: crosshair snaps to the nearest
// recorded sample; the last point is directly labeled. The line breaks
// across observation gaps (cadence-relative; see lib/interp gapBreakMs)
// so unobserved spans render as a break, never a fabricated move. Drawn
// at its measured pixel width (ResizeObserver; 640 until measured), so
// labels stay 12px on a phone and the chart does not grow 700px tall on
// a widescreen. Shared by equity curves, stock charts, P&L history, and
// valuation scenarios.

import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { HistorySeries } from '../lib/types'
import {
  clamp,
  gapBreakMs,
  gapScale,
  nearestIndex,
  scalePos,
  scaleTime,
  splitAtGaps,
} from '../lib/interp'
import { chartGeometry, endLabel, tipTransform } from '../lib/chart'
import { etDateMs, etTimeMs, usdSigned } from '../lib/format'
import { useMeasuredWidth } from '../hooks/useMeasuredWidth'

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
  timeFormat,
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

  const g = chartGeometry(width, [yHi, 0, yLo].map(valueFormat))
  const plotW = g.vw - g.padL - g.padR
  const plotH = g.vh - g.padT - g.padB
  const sy = (v: number): number => g.padT + ((yHi - v) / (yHi - yLo)) * plotH
  // y ticks in draw order, each at least 14px from every tick kept
  // before it (0 and y_lo can sit a hair apart on a mostly-positive
  // series and stack into an unreadable corner); zero keeps its gridline
  // only when the axis actually contains it (level series such as equity
  // or prices sit far from $0)
  const ticks = (yLo <= 0 && 0 <= yHi ? [yHi, 0, yLo] : [yHi, yLo]).filter((v, i, cand) =>
    i === 0 || cand.slice(0, i).every((u) => Math.abs(sy(u) - sy(v)) >= 14),
  )
  // x labels carry the date once the span crosses a day — a time-only
  // pair like "12:53 ET .. 13:18 ET" reads as 25 minutes on a 25-hour
  // series
  const fmtTime =
    timeFormat ??
    ((ts: number) => `${t1Raw - t0 > 86_400_000 ? `${etDateMs(ts)} ` : ''}${etTimeMs(ts)} ET`)
  // gap-compressed axis when the series has observation gaps: each run
  // keeps its share of OBSERVED time, each gap a small allowance — the
  // line still BREAKS across the gap (never a fabricated move), but the
  // chart draws ~all of its width instead of stubs around void
  const breakMs = gapBreakMs(pts)
  const scale = gapScale(pts, breakMs)
  const runs = splitAtGaps(pts, breakMs)
  const segments = runs.filter((s) => s.length > 1)
  // dotted bridges across each compressed gap: the runs read as one
  // series while the dash says "no observation here"
  const bridges = scale
    ? scale.runs.slice(1).map((r, i) => {
        const prev = scale.runs[i]
        const a = runs[i][runs[i].length - 1] // last real sample before the gap
        const b = runs[i + 1][0] // first real sample after it
        return {
          key: `gap-${i}`,
          x1: g.padL + prev.b * plotW,
          y1: sy(a[1]),
          x2: g.padL + r.a * plotW,
          y2: sy(b[1]),
        }
      })
    : []
  const sx = (t: number): number =>
    scale ? g.padL + scalePos(scale, t) * plotW : g.padL + ((t - t0) / (t1 - t0)) * plotW

  const ts = pts.map(([t]) => t)
  const lastPt = pts[pts.length - 1]
  const lastPos = lastPt[1] >= 0
  const lastText = valueFormat(lastPt[1])
  const lastAt = endLabel(sx(lastPt[0]), lastText, g.vw)
  // the flipped-inside (end-anchored) label sits left of the dot where a
  // steep final segment can cross it — push it to the side of the dot
  // away from the incoming slope
  const prevPt = pts[pts.length - 2]
  const lastLabelY =
    sy(lastPt[1]) +
    (lastAt.anchor === 'end' && prevPt !== undefined && prevPt[0] !== lastPt[0]
      ? lastPt[1] >= prevPt[1]
        ? -12
        : 12
      : 0)

  const onPointerMove = (e: ReactPointerEvent<SVGRectElement>): void => {
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    if (rect.width === 0) return
    const vx = clamp(((e.clientX - rect.left) / rect.width) * g.vw, g.padL, g.vw - g.padR)
    const n = (vx - g.padL) / plotW
    const t = scale ? scaleTime(scale, n) : t0 + n * (t1 - t0)
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

        {/* the value line (per observed run), dotted bridges across the
            compressed gaps, + directly labeled last point */}
        {bridges.map((b) => (
          <line
            key={b.key}
            x1={b.x1}
            y1={b.y1}
            x2={b.x2}
            y2={b.y2}
            strokeDasharray="2 5"
            className="gap-bridge"
          />
        ))}
        {segments.map((seg, k) => (
          <polyline
            key={k}
            points={seg.map(([t, v]) => `${sx(t).toFixed(1)},${sy(v).toFixed(1)}`).join(' ')}
            className="payoff-line"
          />
        ))}
        <circle
          cx={sx(lastPt[0])}
          cy={sy(lastPt[1])}
          r={4}
          className={`dot-ring ${lastPos ? 'fill-pos' : 'fill-neg'}`}
        />
        <text
          x={lastAt.x}
          y={lastLabelY}
          textAnchor={lastAt.anchor}
          dominantBaseline="middle"
          className={`plateau-label ${lastPos ? 'fill-pos' : 'fill-neg'}`}
        >
          {lastText}
        </text>

        {/* time axis: first + last sample */}
        <text x={g.padL} y={g.vh - g.padB + 18} className="axis-label">
          {fmtTime(t0)}
        </text>
        <text x={g.vw - g.padR} y={g.vh - g.padB + 18} textAnchor="end" className="axis-label">
          {fmtTime(t1Raw)}
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
          <div className="tip-price num">{fmtTime(hoverPt[0])}</div>
          <div className={`num ${hoverPt[1] >= 0 ? 'pnl-pos' : 'pnl-neg'}`}>
            {valueFormat(hoverPt[1])}
          </div>
        </div>
      )}
    </div>
  )
}
