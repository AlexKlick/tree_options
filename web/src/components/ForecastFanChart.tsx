/** Forecast fan chart (RL-3 Outlook tab) — the latest-origin forward
 * quantile bars for every model, drawn against the last observed
 * close.
 *
 * One SVG on the existing chart layer (chartGeometry + measured
 * width): the last close sits at the origin position, each model's
 * quantile bar sits at the horizon position, and NOTHING is drawn
 * between them — there is no observed path to the target, and the
 * chart must not invent one. Values are index levels (numeric), never
 * currency. An ``unavailable`` fan entry leaves a labeled gap, never a
 * fabricated band.
 */

import { useRef } from 'react'
import { chartGeometry } from '../lib/chart'
import { useMeasuredWidth } from '../hooks/useMeasuredWidth'
import { fanLevels, fmtLevel } from '../lib/forecast'
import type { ForecastForward } from '../lib/types'

const MODEL_COLORS = ['#4f9cf9', '#e8833a', '#3aa88f', '#a25bd6']

export interface ForecastFanChartProps {
  forward: ForecastForward
  /** the declared tau grid, e.g. [0.05, 0.25, 0.5, 0.75, 0.95] */
  quantileGrid: number[]
  ariaLabel: string
  valueFormat?: (v: number) => string
}

interface Bar {
  model: string
  color: string
  /** levels aligned to the declared grid by tau slot; null = that
   * quantile was not published (a gap, never substituted). */
  levels: Array<number | null>
}

/** A model is drawable only when BOTH outer endpoints (the first and
 * last grid slots) are published: a missing q05 must narrow nothing —
 * the model renders as an explicit gap. Eligibility is the SAME test
 * for domain, geometry, and legend (checkpoint C, P2-5). */
function drawableBar(b: Bar): boolean {
  return b.levels[0] !== null && b.levels[b.levels.length - 1] !== null
}

export function ForecastFanChart({
  forward,
  quantileGrid,
  ariaLabel,
  valueFormat = (v) => fmtLevel(v),
}: ForecastFanChartProps): JSX.Element {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const width = useMeasuredWidth(wrapRef)

  const bars: Bar[] = forward.fan.map((entry, i) => ({
    model: entry.model,
    color: MODEL_COLORS[i % MODEL_COLORS.length],
    levels: fanLevels(entry, quantileGrid),
  }))
  const drawable = bars.filter(drawableBar)
  const lo = drawable.flatMap((b) => b.levels.filter((v): v is number => v !== null))
  const domainValues = [...lo, forward.last_close]
  if (drawable.length === 0 || domainValues.length === 0) {
    return (
      <div ref={wrapRef} className="chart-wrap" role="img" aria-label={ariaLabel}>
        <p className="muted">
          no forward quantiles available for this horizon
        </p>
      </div>
    )
  }
  const yHiRaw = Math.max(...domainValues)
  const yLoRaw = Math.min(...domainValues)
  const pad = (yHiRaw - yLoRaw) * 0.08 || 1
  const domain = { hi: yHiRaw + pad, lo: yLoRaw - pad }

  const g = chartGeometry(width, [domain.hi, (domain.hi + domain.lo) / 2, domain.lo].map(valueFormat))
  const plotW = g.vw - g.padL - g.padR
  const plotH = g.vh - g.padT - g.padB
  const sy = (v: number): number =>
    g.padT + ((domain.hi - v) / (domain.hi - domain.lo)) * plotH
  const xOrigin = g.padL + plotW * 0.22
  const xTarget = g.padL + plotW * 0.78
  const ticks = [domain.hi, (domain.hi + domain.lo) / 2, domain.lo]
  const bandW = Math.max(10, plotW * 0.06)
  const mid = Math.floor(quantileGrid.length / 2)   // median slot (τ=0.5 on the 5-slot grid)

  return (
    <div ref={wrapRef} className="chart-wrap" role="img" aria-label={ariaLabel}>
      <svg viewBox={`0 0 ${g.vw} ${g.vh}`} className="chart-svg" data-testid="forecast-fan-svg">
        {ticks.map((v) => (
          <g key={v}>
            <line x1={g.padL} x2={g.vw - g.padR} y1={sy(v)} y2={sy(v)} className="gridline" />
            <text x={g.padL - 6} y={sy(v) + 4} textAnchor="end" className="axis-label">
              {valueFormat(v)}
            </text>
          </g>
        ))}
        {/* the last OBSERVED close — the anchor the bands stand on */}
        <line x1={xOrigin} x2={xTarget} y1={sy(forward.last_close)} y2={sy(forward.last_close)}
          stroke="#8a8f98" strokeWidth={1} strokeDasharray="3 3" />
        <circle cx={xOrigin} cy={sy(forward.last_close)} r={2.5} fill="#8a8f98" />
        <text x={xOrigin - 6} y={sy(forward.last_close) + 4} textAnchor="end" className="axis-label">
          {valueFormat(forward.last_close)}
        </text>
        <text x={xOrigin} y={g.vh - g.padB + 14} textAnchor="middle" className="axis-label">
          last close
        </text>
        <text x={xTarget} y={g.vh - g.padB + 14} textAnchor="middle" className="axis-label">
          +{forward.horizon_sessions} sessions
        </text>
        {bars.map((b) => {
          if (!drawableBar(b)) {
            // an explicit gap: the model's outer band is incomplete
            return (
              <text key={b.model} x={xTarget} y={g.padT + 12} textAnchor="middle"
                className="axis-label" fill={b.color}>
                {b.model}: unavailable
              </text>
            )
          }
          // endpoints by GRID SLOT, never first/last non-null: a missing
          // quantile stays missing instead of narrowing the band
          const qLo = b.levels[0] as number
          const qHi = b.levels[b.levels.length - 1] as number
          const iLo = b.levels[1]
          const iHi = b.levels[b.levels.length - 2]
          const median = b.levels[mid]
          return (
            <g key={b.model} data-testid={`forecast-fan-bar-${b.model}`}>
              {/* outer band: first to last grid slot */}
              <rect x={xTarget - bandW / 2} y={sy(qHi)} width={bandW} height={sy(qLo) - sy(qHi)}
                fill={b.color} fillOpacity={0.18} stroke={b.color} strokeWidth={1} />
              {/* inner band only when BOTH inner slots published */}
              {iLo !== null && iHi !== null && (
                <rect x={xTarget - bandW / 2} y={sy(iHi)} width={bandW}
                  height={sy(iLo) - sy(iHi)}
                  fill={b.color} fillOpacity={0.38} />
              )}
              {/* median: the middle grid slot, when published */}
              {median !== null && (
                <>
                  <line x1={xTarget - bandW / 2 - 4} x2={xTarget + bandW / 2 + 4}
                    y1={sy(median)} y2={sy(median)} stroke={b.color} strokeWidth={2} />
                  <text x={xTarget + bandW / 2 + 7} y={sy(median) + 4} className="axis-label"
                    fill={b.color}>
                    {valueFormat(median)}
                  </text>
                </>
              )}
            </g>
          )
        })}
      </svg>
      <div className="nav-chart-legend">
        {bars.map((b) => (
          <span key={b.model} className="nav-chart-key">
            <span className="swatch" style={{ background: b.color }} aria-hidden />
            <span className="key-label">{b.model}</span>
            <span className="key-value">
              {drawableBar(b)
                ? `${valueFormat(b.levels[0] as number)} – ${valueFormat(b.levels[b.levels.length - 1] as number)}`
                : 'unavailable'}
            </span>
          </span>
        ))}
      </div>
      <p className="muted">
        Quantile bands for the level {forward.horizon_sessions} sessions after
        the origin ({forward.origin_session}); the target session lies beyond
        the observed data, so no realized value exists yet. Bands are nominal
        prediction intervals, not confidence intervals — calibration is stated
        separately as empirical coverage in the receipt table.
      </p>
    </div>
  )
}
