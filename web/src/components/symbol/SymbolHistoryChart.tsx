// Long-term price + volume chart for the symbol page's Price tab (Phase 2
// viewer upgrade): ONE svg, a ~68%-height price band over a ~24% volume
// band on a shared session x-axis. Line mode keeps TimeSeriesChart's
// minimal-ink polyline; candle mode draws a wick high->low plus an
// open<->close body (min body height 1px). The server extent covers
// closes only, so the band re-derives over session highs/lows before
// wicks are mapped. Crosshair + tooltip snap to the hovered session.

import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { useMeasuredWidth } from '../../hooks/useMeasuredWidth'
import { clamp } from '../../lib/interp'
import { endLabel, labelWidth, tipTransform } from '../../lib/chart'
import { compactCount } from '../../lib/format'
import type { OhlcPoint } from '../../lib/types'

export type ChartMode = 'line' | 'candles'

export interface SymbolHistoryChartProps {
  points: OhlcPoint[]
  yLo: number
  yHi: number
  volMax: number
  mode: ChartMode
  ariaLabel: string
  height?: number
}

const dateFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  month: 'short',
  day: 'numeric',
  year: 'numeric',
})

// the price axis fits the data extent, so cheap stocks need cents
const axisPrice = (v: number): string =>
  v >= 100 ? `$${Math.round(v).toLocaleString('en-US')}` : `$${v.toFixed(2)}`

export function SymbolHistoryChart({
  points,
  yLo,
  yHi,
  volMax,
  mode,
  ariaLabel,
  height,
}: SymbolHistoryChartProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const svgRef = useRef<SVGSVGElement | null>(null)
  const [hover, setHover] = useState<{ i: number; cssX: number; cssW: number } | null>(null)
  const width = useMeasuredWidth(wrapRef)

  const n = points.length
  if (n === 0) {
    return <div className="chart-wrap" role="img" aria-label={ariaLabel} />
  }

  // wicks need the highs/lows too — y_lo/y_hi cover closes only
  let lo = yLo
  let hi = yHi
  for (const p of points) {
    if (p[2] > hi) hi = p[2]
    if (p[3] < lo) lo = p[3]
  }
  if (lo === hi) {
    lo -= 1
    hi += 1
  }
  const vMax = volMax > 0 ? volMax : 1

  const vw = Math.max(240, Math.round(width))
  const vh = height ?? Math.round(Math.min(400, Math.max(280, vw * 0.34)))
  const padL = Math.ceil(
    Math.max(
      labelWidth(axisPrice(hi)),
      labelWidth(axisPrice(lo)),
      labelWidth(compactCount(vMax)),
    ) + 12,
  )
  const padR = 14
  const padT = 16
  const padB = 26
  const plotW = vw - padL - padR
  const plotH = vh - padT - padB
  // price ~68% / volume ~24% / the remainder is the breathing gap
  const priceH = Math.round(plotH * 0.68)
  const volH = Math.round(plotH * 0.24)
  const priceTop = padT
  const priceBot = priceTop + priceH
  const volTop = priceBot + (plotH - priceH - volH)
  const volBot = volTop + volH

  // session slots, not wall-clock x: weekends/holidays never stretch the
  // axis and candle bodies stay one uniform width
  const slot = plotW / n
  const xAt = (i: number): number => padL + (i + 0.5) * slot
  const sy = (v: number): number => priceTop + ((hi - v) / (hi - lo)) * priceH
  const vy = (v: number): number => volBot - (Math.min(v, vMax) / vMax) * volH
  const barW = Math.max(1, Math.min(slot * 0.7, 9))

  const lastPt = points[n - 1]
  const lastUp = lastPt[4] >= lastPt[1]
  const lastText = axisPrice(lastPt[4])
  const lastAt = endLabel(xAt(n - 1), lastText, vw)

  const line =
    mode === 'line'
      ? points.map((p, i) => `${xAt(i).toFixed(1)},${sy(p[4]).toFixed(1)}`).join(' ')
      : null

  const onPointerMove = (e: ReactPointerEvent<SVGRectElement>): void => {
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    if (rect.width === 0) return
    const vx = clamp(((e.clientX - rect.left) / rect.width) * vw, padL, vw - padR)
    // nearest session slot center (slots are uniform, so arithmetic is exact)
    const i = clamp(Math.round((vx - padL) / slot - 0.5), 0, n - 1)
    setHover({ i, cssX: (xAt(i) / vw) * rect.width, cssW: rect.width })
  }
  const onPointerLeave = (): void => setHover(null)

  const hp = hover ? points[hover.i] : null

  return (
    <div ref={wrapRef} className="chart-wrap" role="img" aria-label={ariaLabel}>
      <svg ref={svgRef} viewBox={`0 0 ${vw} ${vh}`} className="chart-svg" aria-hidden="true">
        {/* price band: extent gridlines + labels */}
        {[hi, lo].map((v) => (
          <g key={v}>
            <line x1={padL} x2={vw - padR} y1={sy(v)} y2={sy(v)} className="gridline" />
            <text
              x={padL - 8}
              y={sy(v)}
              textAnchor="end"
              dominantBaseline="middle"
              className="axis-label"
            >
              {axisPrice(v)}
            </text>
          </g>
        ))}

        {/* volume band: the cap gridline carries its max label */}
        <line x1={padL} x2={vw - padR} y1={volTop} y2={volTop} className="gridline" />
        <text
          x={padL - 8}
          y={volTop}
          textAnchor="end"
          dominantBaseline="middle"
          className="axis-label"
        >
          {compactCount(vMax)}
        </text>
        <line x1={padL} x2={vw - padR} y1={volBot} y2={volBot} className="gridline" />

        {/* volume, both modes */}
        {points.map((p, i) => (
          <rect
            key={p[0]}
            className="vol-bar"
            x={xAt(i) - barW / 2}
            y={vy(p[5])}
            width={barW}
            height={volBot - vy(p[5])}
          />
        ))}

        {/* the price series itself */}
        {line !== null ? (
          <polyline className="payoff-line" points={line} />
        ) : (
          points.map((p, i) => {
            const up = p[4] >= p[1]
            return (
              <g key={p[0]} className={up ? 'candle-up' : 'candle-down'}>
                <line
                  className="candle-wick"
                  x1={xAt(i)}
                  x2={xAt(i)}
                  y1={sy(p[2])}
                  y2={sy(p[3])}
                />
                <rect
                  className="candle-body"
                  x={xAt(i) - barW / 2}
                  y={sy(Math.max(p[1], p[4]))}
                  width={barW}
                  height={Math.max(1, Math.abs(sy(p[1]) - sy(p[4])))}
                />
              </g>
            )
          })
        )}

        {/* last close, directly labeled (color = last session direction) */}
        <circle
          cx={xAt(n - 1)}
          cy={sy(lastPt[4])}
          r={4}
          className={`dot-ring ${lastUp ? 'fill-pos' : 'fill-neg'}`}
        />
        <text
          x={lastAt.x}
          y={sy(lastPt[4])}
          textAnchor={lastAt.anchor}
          dominantBaseline="middle"
          className={`plateau-label ${lastUp ? 'fill-pos' : 'fill-neg'}`}
        >
          {lastText}
        </text>

        {/* time axis: first + last session */}
        <text x={padL} y={vh - padB + 18} className="axis-label">
          {dateFmt.format(new Date(points[0][0]))}
        </text>
        <text x={vw - padR} y={vh - padB + 18} textAnchor="end" className="axis-label">
          {dateFmt.format(new Date(lastPt[0]))}
        </text>

        {/* crosshair spans both bands — one shared session axis */}
        {hover && hp && (
          <g>
            <line
              x1={xAt(hover.i)}
              x2={xAt(hover.i)}
              y1={padT}
              y2={vh - padB}
              className="crosshair"
            />
            <circle cx={xAt(hover.i)} cy={sy(hp[4])} r={4} className="fill-ink dot-ring" />
          </g>
        )}

        <rect
          x={padL}
          y={padT}
          width={plotW}
          height={vh - padT}
          fill="transparent"
          style={{ touchAction: 'none' }}
          onPointerMove={onPointerMove}
          onPointerLeave={onPointerLeave}
        />
      </svg>

      {hover && hp && (
        <div
          className="chart-tip"
          style={{
            left: hover.cssX,
            transform: tipTransform(hover.cssX, hover.cssW, 0.6),
          }}
        >
          <div className="tip-price num">{dateFmt.format(new Date(hp[0]))}</div>
          <div className="num">
            O {hp[1].toFixed(2)} · H {hp[2].toFixed(2)} · L {hp[3].toFixed(2)}
          </div>
          <div className={`num ${hp[4] >= hp[1] ? 'pnl-pos' : 'pnl-neg'}`}>
            C {hp[4].toFixed(2)} · Vol {compactCount(hp[5])}
          </div>
        </div>
      )}
    </div>
  )
}
