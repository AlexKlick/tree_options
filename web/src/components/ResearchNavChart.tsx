/** Multi-line dollar-value chart for the Research comparison
 * workspace — one SVG, shared cursor, per-series legend values.
 *
 * Built on the existing SVG chart layer (chartGeometry, the measured-
 * width hook, the 12px-label idiom of TimeSeriesChart) rather than a
 * new charting dependency. ``null`` values break a series' line: a
 * missing mark is a visible gap, never a zero and never a fabricated
 * move to the next observation.
 */

import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { chartGeometry } from '../lib/chart'
import { useMeasuredWidth } from '../hooks/useMeasuredWidth'
import { usdSigned } from '../lib/format'

export interface NavSeries {
  id: string
  label: string
  color: string
  /** [ts_ms, value | null] over the shared session dates */
  points: [number, number | null][]
}

export interface ResearchNavChartProps {
  series: NavSeries[]
  ariaLabel: string
  valueFormat?: (v: number) => string
  /** fires on cursor move/leave with the hovered timestamp (ms) */
  onCursor?: (ts: number | null) => void
}

interface Segment {
  key: string
  d: string
  lastX: number
  lastY: number
  lastV: number
}

function segmentsFor(
  points: [number, number | null][],
  sx: (t: number) => number,
  sy: (v: number) => number,
  id: string,
): Segment[] {
  const segs: Segment[] = []
  let current: { xs: number[]; ys: number[] } | null = null
  const flush = (): void => {
    if (current && current.xs.length > 1) {
      const d = current.xs.map((x, i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${current!.ys[i].toFixed(1)}`).join('')
      segs.push({
        key: `${id}-${segs.length}`,
        d,
        lastX: current.xs[current.xs.length - 1],
        lastY: current.ys[current.ys.length - 1],
        lastV: 0, // filled by caller with the real value
      })
    }
    current = null
  }
  points.forEach(([t, v]) => {
    if (v === null) {
      flush()
      return
    }
    if (!current) current = { xs: [], ys: [] }
    current.xs.push(sx(t))
    current.ys.push(sy(v))
  })
  flush()
  return segs
}

export function ResearchNavChart({
  series,
  ariaLabel,
  valueFormat = usdSigned,
  onCursor,
}: ResearchNavChartProps): JSX.Element {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const [hoverTs, setHoverTs] = useState<number | null>(null)
  const width = useMeasuredWidth(wrapRef)

  const allPoints = series.flatMap((s) => s.points)
  const values = allPoints.filter((p): p is [number, number] => p[1] !== null)
  const tsAll = Array.from(new Set(allPoints.map(([t]) => t))).sort((a, b) => a - b)
  if (tsAll.length === 0 || values.length === 0) {
    return (
      <div ref={wrapRef} className="chart-wrap" role="img" aria-label={ariaLabel}>
        <p className="muted">no observations in this window</p>
      </div>
    )
  }
  const t0 = tsAll[0]
  const t1 = tsAll[tsAll.length - 1] === t0 ? t0 + 1 : tsAll[tsAll.length - 1]
  const yHi = Math.max(...values.map(([, v]) => v))
  const yLo = Math.min(...values.map(([, v]) => v))
  const pad = (yHi - yLo) * 0.05 || 1
  const domain = { hi: yHi + pad, lo: yLo - pad }

  const g = chartGeometry(width, [domain.hi, 0, domain.lo].map(valueFormat))
  const plotW = g.vw - g.padL - g.padR
  const plotH = g.vh - g.padT - g.padB
  const sx = (t: number): number => g.padL + ((t - t0) / (t1 - t0)) * plotW
  const sy = (v: number): number => g.padT + ((domain.hi - v) / (domain.hi - domain.lo)) * plotH
  const ticks = [domain.hi, (domain.hi + domain.lo) / 2, domain.lo]

  const onPointerMove = (e: ReactPointerEvent<SVGRectElement>): void => {
    const t = t0 + ((e.clientX - e.currentTarget.getBoundingClientRect().left) / e.currentTarget.getBoundingClientRect().width) * (t1 - t0)
    // nearest shared date
    let best = tsAll[0]
    for (const d of tsAll) {
      if (Math.abs(d - t) < Math.abs(best - t)) best = d
    }
    setHoverTs(best)
    onCursor?.(best)
  }
  const onPointerLeave = (): void => {
    setHoverTs(null)
    onCursor?.(null)
  }

  const hoverValues = hoverTs === null ? [] : series.map((s) => {
    const hit = s.points.find(([t]) => t === hoverTs)
    return { id: s.id, label: s.label, color: s.color, value: hit ? hit[1] : undefined }
  })

  return (
    <div ref={wrapRef} className="chart-wrap" role="img" aria-label={ariaLabel}>
      <svg viewBox={`0 0 ${g.vw} ${g.vh}`} className="chart-svg">
        {ticks.map((v) => (
          <g key={v}>
            <line x1={g.padL} x2={g.vw - g.padR} y1={sy(v)} y2={sy(v)} className="gridline" />
            <text x={g.padL - 6} y={sy(v) + 4} textAnchor="end" className="axis-label">
              {valueFormat(v)}
            </text>
          </g>
        ))}
        {series.map((s) => (
          <g key={s.id}>
            {segmentsFor(s.points, sx, sy, s.id).map((seg) => (
              <path key={seg.key} d={seg.d} fill="none" stroke={s.color} strokeWidth={2} />
            ))}
            {/* last observed value dot + label */}
            {[...s.points].reverse().find(([, v]) => v !== null) && (() => {
              const idx = s.points.reduce((acc, [, v], i) => (v !== null ? i : acc), 0)
              const [lt, lv] = s.points[idx] as [number, number]
              return (
                <g>
                  <circle cx={sx(lt)} cy={sy(lv)} r={2.5} fill={s.color} />
                  <text x={sx(lt) + 5} y={sy(lv) + 4} className="axis-label" fill={s.color}>
                    {valueFormat(lv)}
                  </text>
                </g>
              )
            })()}
          </g>
        ))}
        {hoverTs !== null && (
          <line x1={sx(hoverTs)} x2={sx(hoverTs)} y1={g.padT} y2={g.vh - g.padB} className="nav-cursor" />
        )}
        <rect
          x={g.padL} y={g.padT} width={plotW} height={plotH}
          fill="transparent" style={{ touchAction: 'pan-y' }}
          onPointerMove={onPointerMove}
          onPointerLeave={onPointerLeave}
        />
      </svg>
      <div className="nav-chart-legend">
        {(hoverValues.length ? hoverValues : series.map((s) => (
          { id: s.id, label: s.label, color: s.color, value: undefined as number | undefined | null }
        ))).map((entry) => (
          <span key={entry.id} className="nav-chart-key">
            <span className="swatch" style={{ background: entry.color }} aria-hidden />
            <span className="key-label">{entry.label}</span>
            <span className="key-value">
              {entry.value === undefined || entry.value === null ? '— gap' : valueFormat(entry.value)}
            </span>
          </span>
        ))}
      </div>
    </div>
  )
}
