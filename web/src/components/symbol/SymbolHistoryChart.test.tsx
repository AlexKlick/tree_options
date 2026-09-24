import { describe, expect, it } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import type { OhlcPoint } from '../../lib/types'
import { SymbolHistoryChart } from './SymbolHistoryChart'

// four sessions with a doji (open===close) on the third; highs/lows exceed
// the server's close-only extent, pinning the client-side band expansion
const points: OhlcPoint[] = [
  [86_400_000, 10.0, 12.0, 9.0, 11.0, 1_000],
  [172_800_000, 11.0, 11.5, 8.5, 9.0, 2_000_000],
  [259_200_000, 9.0, 9.5, 7.5, 9.0, 15_000_000],
  [345_600_000, 9.2, 13.0, 9.1, 12.5, 940_000],
]

// jsdom has no ResizeObserver, so the chart draws at the 640 default width;
// from the fixture: extent expands to hi=13/lo=7.5, padL=56, vh=280,
// price band [16,178], volume band [197,254]
const PRICE_TOP = 16
const PRICE_BOT = 178
const VOL_TOP = 197
const VOL_BOT = 254

/** viewBox "minx miny w h" -> [w, h]. */
const viewBoxSize = (container: HTMLElement): [number, number] => {
  const parts = container.querySelector('svg')!.getAttribute('viewBox')!.split(' ').map(Number)
  return [parts[2], parts[3]]
}

const yExtent = (els: Element[]): { min: number; max: number } => {
  const ys: number[] = []
  for (const el of els) {
    if (el.tagName === 'line') {
      ys.push(Number(el.getAttribute('y1')), Number(el.getAttribute('y2')))
    } else if (el.tagName === 'polyline') {
      for (const pair of (el.getAttribute('points') ?? '').split(' ')) {
        ys.push(Number(pair.split(',')[1]))
      }
    }
  }
  return { min: Math.min(...ys), max: Math.max(...ys) }
}

describe('SymbolHistoryChart', () => {
  it('draws one dual-band svg: line + volume bars, no candle bodies', () => {
    const { container } = render(
      <SymbolHistoryChart
        points={points}
        yLo={9.0}
        yHi={11.0}
        volMax={15_000_000}
        mode="line"
        ariaLabel="SPY 3Y price"
      />,
    )
    expect(screen.getByRole('img', { name: 'SPY 3Y price' })).toBeTruthy()

    // geometry: 640-wide viewBox, price band above the volume band
    const [vw, vh] = viewBoxSize(container)
    expect(vw).toBe(640)
    expect(vh).toBe(280)

    const line = container.querySelector('polyline.payoff-line')
    expect(line).not.toBeNull()
    const price = yExtent([line!])
    expect(price.min).toBeGreaterThanOrEqual(PRICE_TOP - 0.1)
    expect(price.max).toBeLessThanOrEqual(PRICE_BOT + 0.1)

    const bars = [...container.querySelectorAll('.vol-bar')]
    expect(bars.length).toBe(points.length)
    for (const b of bars) {
      expect(Number(b.getAttribute('y')) + Number(b.getAttribute('height'))).toBeCloseTo(VOL_BOT, 5)
      expect(Number(b.getAttribute('y'))).toBeGreaterThanOrEqual(VOL_TOP - 0.1)
    }

    expect(container.querySelector('.candle-body')).toBeNull()

    // extent labels land inside their bands
    const labels = [...container.querySelectorAll('text.axis-label')].map((t) => t.textContent)
    expect(labels).toContain('$13.00')
    expect(labels).toContain('$7.50')
    expect(labels).toContain('15M')
  })

  it('honors the height prop (viewBox tracks it)', () => {
    const { container } = render(
      <SymbolHistoryChart
        points={points}
        yLo={9.0}
        yHi={11.0}
        volMax={15_000_000}
        mode="line"
        ariaLabel="h"
        height={400}
      />,
    )
    expect(viewBoxSize(container)[1]).toBe(400)
  })

  it('draws wick + body per session in candle mode (min body 1px), no polyline', () => {
    const { container } = render(
      <SymbolHistoryChart
        points={points}
        yLo={9.0}
        yHi={11.0}
        volMax={15_000_000}
        mode="candles"
        ariaLabel="SPY candles"
      />,
    )
    expect(container.querySelector('polyline.payoff-line')).toBeNull()

    const wicks = [...container.querySelectorAll('.candle-wick')]
    const bodies = [...container.querySelectorAll('.candle-body')]
    expect(wicks.length).toBe(points.length)
    expect(bodies.length).toBe(points.length)

    // the wick domain expanded over highs/lows: nothing clips above the band
    expect(yExtent(wicks).min).toBeGreaterThanOrEqual(PRICE_TOP - 0.1)

    for (let i = 0; i < bodies.length; i++) {
      const body = bodies[i]
      const wick = wicks[i]
      const y = Number(body.getAttribute('y'))
      const h = Number(body.getAttribute('height'))
      expect(h).toBeGreaterThanOrEqual(1)
      const w = yExtent([wick])
      expect(w.min).toBeLessThanOrEqual(y + 0.1) // wick tops at/above the body
      expect(w.max).toBeGreaterThanOrEqual(y + h - 0.1) // and reaches below it
    }

    // the doji session (open===close) keeps a 1px body
    expect(Number(bodies[2].getAttribute('height'))).toBe(1)

    // session direction colors: down candle, up candle
    expect(bodies[1].closest('g')!.getAttribute('class')).toBe('candle-down')
    expect(bodies[3].closest('g')!.getAttribute('class')).toBe('candle-up')
  })

  it('snaps a crosshair over both bands and shows the OHLCV tooltip', () => {
    const { container } = render(
      <SymbolHistoryChart
        points={points}
        yLo={9.0}
        yHi={11.0}
        volMax={15_000_000}
        mode="line"
        ariaLabel="SPY 3Y price"
      />,
    )
    const svg = container.querySelector('svg')!
    svg.getBoundingClientRect = () =>
      ({ left: 0, top: 0, right: 640, bottom: 280, width: 640, height: 280, x: 0, y: 0 }) as DOMRect
    const overlay = container.querySelector('rect[fill="transparent"]')!

    expect(container.querySelector('.chart-tip')).toBeNull()
    // clientX 300 lands in session slot 1 (ts 172800000 = Jan 2 19:00 ET)
    act(() => overlay.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: 300 })))

    const cross = container.querySelector('.crosshair') as SVGLineElement
    expect(cross).not.toBeNull()
    expect(Number(cross.getAttribute('y1'))).toBe(PRICE_TOP)
    expect(Number(cross.getAttribute('y2'))).toBe(VOL_BOT)

    const tip = container.querySelector('.chart-tip')!
    expect(tip.textContent).toContain('Jan 2, 1970')
    expect(tip.textContent).toContain('O 11.00')
    expect(tip.textContent).toContain('H 11.50')
    expect(tip.textContent).toContain('L 8.50')
    expect(tip.textContent).toContain('C 9.00')
    expect(tip.textContent).toContain('Vol 2M')

    // React derives pointerleave from the delegated pointerout
    act(() =>
      overlay.dispatchEvent(new MouseEvent('pointerout', { bubbles: true, relatedTarget: null })),
    )
    expect(container.querySelector('.chart-tip')).toBeNull()
    expect(container.querySelector('.crosshair')).toBeNull()
  })

  it('labels the last close directly at the line end', () => {
    const { container } = render(
      <SymbolHistoryChart
        points={points}
        yLo={9.0}
        yHi={11.0}
        volMax={15_000_000}
        mode="line"
        ariaLabel="SPY 3Y price"
      />,
    )
    const label = container.querySelector('text.plateau-label')!
    expect(label.textContent).toBe('$12.50')
  })
})
