// Interactive payoff chart: hover crosshair + click/drag what-if marker,
// keyboard-accessible as a slider over the price range. Fixed-viewBox SVG;
// every position derives from the server series, so hover/what-if values
// are exact (the server emits the kinks).

import {
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import type { Payoff } from '../lib/types'
import { clamp, interpAt } from '../lib/interp'
import { payoffRegions, type PixelScale } from '../lib/regions'
import { usd2, usdSigned } from '../lib/format'

const VW = 640
const VH = 240
const PAD = { l: 64, r: 20, t: 18, b: 30 }
const STEP = 0.5
const BIG_STEP = 5

const quantize = (v: number): number => Math.round(v / STEP) * STEP

interface Hover {
  price: number
  pnl: number
  cssX: number
  cssW: number
}

export function PayoffChart({ payoff }: { payoff: Payoff }) {
  const { view, points, levels } = payoff
  const svgRef = useRef<SVGSVGElement | null>(null)
  const [hover, setHover] = useState<Hover | null>(null)
  const [whatIf, setWhatIf] = useState<number | null>(null)
  const [dragging, setDragging] = useState(false)

  const pnls = points.map(([, y]) => y)
  const yLoRaw = Math.min(0, ...pnls)
  const yHiRaw = Math.max(0, ...pnls)
  const flat = yLoRaw === yHiRaw
  // Debit-spread losses are tiny next to the max gain, so a purely
  // proportional axis buries the zero line and the loss region against
  // the bottom edge. Keep headroom above the max gain and give the
  // below-zero band a minimum share of the range.
  const yLo = flat
    ? yLoRaw - 1
    : Math.min(
        yLoRaw - Math.max(Math.abs(yLoRaw) * 0.1, 2),
        yLoRaw < 0 ? -yHiRaw * 0.06 : -2,
      )
  const yHi = flat ? yHiRaw + 1 : yHiRaw + Math.max(Math.abs(yHiRaw) * 0.05, 2)

  const plotW = VW - PAD.l - PAD.r
  const plotH = VH - PAD.t - PAD.b
  const sx = (v: number): number =>
    PAD.l + ((v - view.x_lo) / (view.x_hi - view.x_lo)) * plotW
  const sy = (v: number): number =>
    PAD.t + ((yHi - v) / (yHi - yLo)) * plotH
  const invX = (px: number): number =>
    view.x_lo + ((px - PAD.l) / plotW) * (view.x_hi - view.x_lo)
  const scale: PixelScale = { sx, sy, zeroY: sy(0) }
  const regions = payoffRegions(points, scale)
  const line = points.map(([x, y]) => `${sx(x).toFixed(1)},${sy(y).toFixed(1)}`).join(' ')

  const defaultPrice = levels.spot ?? (view.x_lo + view.x_hi) / 2
  const value = whatIf ?? defaultPrice
  const valuePnl = interpAt(points, value)

  const hitFromEvent = (
    e: ReactPointerEvent<SVGRectElement>,
  ): { price: number; cssX: number; cssW: number } | null => {
    const svg = svgRef.current
    if (!svg) return null
    const rect = svg.getBoundingClientRect()
    if (rect.width === 0) return null
    const vx = clamp(((e.clientX - rect.left) / rect.width) * VW, 0, VW)
    return {
      price: clamp(invX(vx), view.x_lo, view.x_hi),
      cssX: (vx / VW) * rect.width,
      cssW: rect.width,
    }
  }

  const onPointerDown = (e: ReactPointerEvent<SVGRectElement>): void => {
    e.currentTarget.setPointerCapture(e.pointerId)
    setDragging(true)
    const hit = hitFromEvent(e)
    if (hit) setWhatIf(quantize(hit.price))
  }
  const onPointerMove = (e: ReactPointerEvent<SVGRectElement>): void => {
    const hit = hitFromEvent(e)
    if (!hit) return
    if (dragging) setWhatIf(quantize(hit.price))
    setHover({
      price: hit.price,
      pnl: interpAt(points, hit.price),
      cssX: hit.cssX,
      cssW: hit.cssW,
    })
  }
  const onPointerUp = (): void => setDragging(false)
  const onPointerLeave = (): void => setHover(null)

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    const step = e.shiftKey ? BIG_STEP : STEP
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp') {
      setWhatIf(quantize(clamp(value + step, view.x_lo, view.x_hi)))
      e.preventDefault()
    } else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') {
      setWhatIf(quantize(clamp(value - step, view.x_lo, view.x_hi)))
      e.preventDefault()
    } else if (e.key === 'Home') {
      setWhatIf(quantize(view.x_lo))
      e.preventDefault()
    } else if (e.key === 'End') {
      setWhatIf(quantize(view.x_hi))
      e.preventDefault()
    } else if (e.key === 'Escape') {
      setWhatIf(null)
    }
  }

  // Direct labels: plateau values at the flat ends, anchored inward.
  const gainY = Math.max(...pnls)
  const lossY = Math.min(...pnls)
  const gainLabelAbove = sy(gainY) > PAD.t + 14
  const lossLabelBelow = sy(lossY) < VH - PAD.b - 14
  const beX = sx(levels.breakeven)
  const bePct = beX / VW
  const beAnchor = bePct > 0.66 ? 'end' : bePct < 0.34 ? 'start' : 'middle'

  // Strike ticks: label both, but drop one when they collide in pixels.
  const ticks = [levels.short_strike, levels.long_strike].filter(
    (v, i, a) => v >= view.x_lo && v <= view.x_hi && a.indexOf(v) === i,
  )
  const tickLabels: number[] = []
  for (const t of ticks) {
    if (tickLabels.every((u) => Math.abs(sx(u) - sx(t)) > 36)) tickLabels.push(t)
  }

  const pinPct = (sx(value) / VW) * 100
  const pinShift = pinPct < 15 ? '0%' : pinPct > 85 ? '-100%' : '-50%'

  return (
    <div
      className="chart-wrap"
      role="slider"
      tabIndex={0}
      aria-label={`${payoff.underlying} price what-if for the ${levels.long_strike}/${levels.short_strike} put spread`}
      aria-valuemin={view.x_lo}
      aria-valuemax={view.x_hi}
      aria-valuenow={value}
      aria-valuetext={`${usd2(value)} at expiry -> ${usdSigned(valuePnl)}`}
      onKeyDown={onKeyDown}
    >
      <svg ref={svgRef} viewBox={`0 0 ${VW} ${VH}`} className="chart-svg" aria-hidden="true">
        {/* frame gridlines (unlabeled; the direct plateau labels carry the
            extremes) + the zero reference, the one labeled y tick */}
        {[yHi, yLo].map((v) => (
          <line
            key={v}
            x1={PAD.l}
            x2={VW - PAD.r}
            y1={sy(v)}
            y2={sy(v)}
            className="gridline"
          />
        ))}
        <line
          x1={PAD.l}
          x2={VW - PAD.r}
          y1={scale.zeroY}
          y2={scale.zeroY}
          className="zero-line"
        />
        <text
          x={PAD.l - 8}
          y={scale.zeroY}
          textAnchor="end"
          dominantBaseline="middle"
          className="axis-label"
        >
          $0
        </text>

        {/* pos/neg fills between the line and the zero baseline */}
        {regions.map((r) => (
          <polygon key={r.sign} points={r.polygon} className={`region-${r.sign}`} />
        ))}

        {/* the payoff line */}
        <polyline points={line} className="payoff-line" />

        {/* strike ticks + labels */}
        {ticks.map((t) => (
          <line
            key={t}
            x1={sx(t)}
            x2={sx(t)}
            y1={VH - PAD.b}
            y2={VH - PAD.b + 5}
            className="strike-tick"
          />
        ))}
        {tickLabels.map((t) => (
          <text
            key={t}
            x={sx(t)}
            y={VH - PAD.b + 18}
            textAnchor="middle"
            className="axis-label"
          >
            {t}
          </text>
        ))}

        {/* breakeven dot on the zero line */}
        <circle cx={beX} cy={scale.zeroY} r={4} className="fill-ink dot-ring" />
        <text
          x={beX}
          y={scale.zeroY + (bePct > 0.5 ? -8 : 16)}
          textAnchor={beAnchor}
          className="plateau-label fill-ink"
        >
          BE {usd2(levels.breakeven)}
        </text>

        {/* plateau labels */}
        <text
          x={PAD.l + 6}
          y={sy(gainY) + (gainLabelAbove ? -8 : 16)}
          className="plateau-label fill-pos"
        >
          {payoff.labels.max_gain}
        </text>
        <text
          x={VW - PAD.r - 6}
          y={sy(lossY) + (lossLabelBelow ? 18 : -8)}
          textAnchor="end"
          className="plateau-label fill-neg"
        >
          {payoff.labels.max_loss}
        </text>

        {/* spot reference (when the account has equity quotes) */}
        {levels.spot !== null &&
          levels.spot >= view.x_lo &&
          levels.spot <= view.x_hi && (
            <g>
              <line
                x1={sx(levels.spot)}
                x2={sx(levels.spot)}
                y1={PAD.t}
                y2={VH - PAD.b}
                className="spot-line"
              />
              <text
                x={sx(levels.spot)}
                y={PAD.t - 5}
                textAnchor={sx(levels.spot) > VW * 0.66 ? 'end' : 'start'}
                className="axis-label fill-accent"
              >
                spot {usd2(levels.spot)}
              </text>
            </g>
          )}

        {/* what-if marker */}
        {whatIf !== null && (
          <g>
            <line
              x1={sx(value)}
              x2={sx(value)}
              y1={PAD.t}
              y2={VH - PAD.b}
              className="whatif-line"
            />
            <circle
              cx={sx(value)}
              cy={sy(valuePnl)}
              r={5}
              className="dot-ring whatif-dot"
            />
          </g>
        )}

        {/* hover crosshair */}
        {hover && (
          <g>
            <line
              x1={sx(hover.price)}
              x2={sx(hover.price)}
              y1={PAD.t}
              y2={VH - PAD.b}
              className="crosshair"
            />
            <circle
              cx={sx(hover.price)}
              cy={sy(hover.pnl)}
              r={4}
              className="fill-ink dot-ring"
            />
          </g>
        )}

        {/* event surface (last: sits above everything) */}
        <rect
          x={PAD.l}
          y={PAD.t}
          width={plotW}
          height={plotH + PAD.b}
          fill="transparent"
          style={{ touchAction: 'none' }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerLeave={onPointerLeave}
        />
      </svg>

      {whatIf !== null && (
        <div
          className={`chart-pin ${valuePnl >= 0 ? 'pnl-pos' : 'pnl-neg'}`}
          style={{ left: `${pinPct}%`, transform: `translateX(${pinShift})` }}
        >
          {usd2(value)} → {usdSigned(valuePnl)}
        </div>
      )}

      {hover && (
        <div
          className="chart-tip"
          style={{
            left: hover.cssX,
            transform:
              hover.cssX > hover.cssW * 0.66
                ? 'translateX(calc(-100% - 12px))'
                : 'translateX(12px)',
          }}
        >
          <div className="tip-price num">{usd2(hover.price)}</div>
          <div className={`num ${hover.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}`}>
            {usdSigned(hover.pnl)}
          </div>
        </div>
      )}
    </div>
  )
}
