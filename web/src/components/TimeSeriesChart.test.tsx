import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render } from '@testing-library/react'
import type { HistorySeries } from '../lib/types'
import { labelWidth } from '../lib/chart'
import { usdLevel } from '../lib/format'
import { TimeSeriesChart } from './TimeSeriesChart'

const equity: HistorySeries = {
  points: [
    [1_790_116_119_162, 1_000_276.02],
    [1_790_120_644_155, 1_000_270.16],
  ],
  y_lo: 1_000_269.2,
  y_hi: 1_000_277,
  last: { ts_ms: 1_790_120_644_155, value: 1_000_270.16, pos: true },
}

const pnl: HistorySeries = {
  points: [
    [1_000, -4],
    [2_000, 6],
  ],
  y_lo: -4,
  y_hi: 6,
  last: { ts_ms: 2_000, pnl: 6, pos: true },
}

const viewBoxWidth = (svg: SVGSVGElement): number =>
  Number(svg.getAttribute('viewBox')!.split(' ')[2])

describe('TimeSeriesChart (M8 flash review)', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('draws no $0 line or label for a level series that never nears zero', () => {
    const { container } = render(
      <TimeSeriesChart series={equity} ariaLabel="equity" valueFormat={usdLevel} />,
    )
    expect(container.querySelector('.zero-line')).toBeNull()
    const labels = [...container.querySelectorAll('text')].map((t) => t.textContent)
    expect(labels).not.toContain('$0')
  })

  it('keeps the zero line for a P&L series that crosses zero', () => {
    const { container } = render(<TimeSeriesChart series={pnl} ariaLabel="pnl" />)
    expect(container.querySelector('.zero-line')).not.toBeNull()
  })

  it('breaks the line across an observation gap instead of bridging it', () => {
    // 20s monitor ticks with a 17h overnight hold between them
    const gapped: HistorySeries = {
      points: [
        [0, 10],
        [20_000, 11],
        [40_000, 12],
        [61_200_000, 13],
        [61_220_000, 14],
      ],
      y_lo: 10,
      y_hi: 14,
      last: { ts_ms: 61_220_000, pnl: 14, pos: true },
    }
    const { container } = render(<TimeSeriesChart series={gapped} ariaLabel="pnl" />)
    const lines = container.querySelectorAll('polyline.payoff-line')
    expect(lines.length).toBe(2) // one per observed run, never a bridge
    const first = lines[0].getAttribute('points')!.split(' ')
    expect(first.length).toBe(3)
  })

  it('compresses the gaps instead of leaving the axis mostly void', () => {
    const gapped: HistorySeries = {
      points: [
        [0, 10],
        [20_000, 11],
        [40_000, 12],
        [61_200_000, 13],
        [61_220_000, 14],
      ],
      y_lo: 10,
      y_hi: 14,
      last: { ts_ms: 61_220_000, pnl: 14, pos: true },
    }
    const { container } = render(<TimeSeriesChart series={gapped} ariaLabel="pnl" />)
    const svg = container.querySelector('svg')!
    const vb = Number(svg.getAttribute('viewBox')!.split(' ')[2])
    const lines = [...container.querySelectorAll('polyline.payoff-line')]
    const segX = lines
      .map((l) =>
        l
          .getAttribute('points')!
          .split(' ')
          .map((p) => Number(p.split(',')[0])),
      )
      .map((xs) => [Math.min(...xs), Math.max(...xs)] as const)
      .sort((a, b) => a[0] - b[0])
    const voids = segX.slice(1).reduce((s, [a], i) => s + (a - segX[i][1]), 0)
    expect(voids / vb).toBeLessThan(0.1) // ~3% per gap, not 70% of nothing
    expect(segX[0][0]).toBeLessThanOrEqual(66) // first run starts at the left pad
  })

  it('bridges the compressed gaps with a dotted connector, never a solid line', () => {
    const gapped: HistorySeries = {
      points: [
        [0, 10],
        [20_000, 11],
        [40_000, 12],
        [61_200_000, 13],
        [61_220_000, 14],
      ],
      y_lo: 10,
      y_hi: 14,
      last: { ts_ms: 61_220_000, pnl: 14, pos: true },
    }
    const { container } = render(<TimeSeriesChart series={gapped} ariaLabel="pnl" />)
    const bridges = container.querySelectorAll('line.gap-bridge')
    expect(bridges.length).toBe(1) // one per compressed gap
    const b = bridges[0]
    // it connects the last sample of run 1 to the first sample of run 2
    expect(Number(b.getAttribute('x1'))).toBeLessThan(Number(b.getAttribute('x2')))
    expect(b.getAttribute('stroke-dasharray')).toBeTruthy() // visually "no data"
    // and a gapless series draws none
    const smooth: HistorySeries = {
      points: [
        [0, 1],
        [20_000, 2],
        [40_000, 3],
      ],
      y_lo: 1,
      y_hi: 3,
      last: { ts_ms: 40_000, pnl: 3, pos: true },
    }
    const { container: c2 } = render(<TimeSeriesChart series={smooth} ariaLabel="pnl" />)
    expect(c2.querySelectorAll('line.gap-bridge').length).toBe(0)
  })

  it('labels the x axis with dates once the span crosses a day', () => {
    const dayPlus: HistorySeries = {
      points: [
        [Date.UTC(2026, 8, 23, 16), 1],
        [Date.UTC(2026, 8, 23, 16) + 20_000, 2],
        [Date.UTC(2026, 8, 24, 17), 3],
      ],
      y_lo: 1,
      y_hi: 3,
      last: { ts_ms: Date.UTC(2026, 8, 24, 17), pnl: 3, pos: true },
    }
    const { container } = render(<TimeSeriesChart series={dayPlus} ariaLabel="pnl" />)
    const labels = [...container.querySelectorAll('text.axis-label')].map((t) => t.textContent)
    const timeAxis = labels.filter((l) => l?.includes('ET'))
    expect(timeAxis.length).toBe(2)
    expect(timeAxis[0]).toMatch(/Sep 2[34] \d{2}:\d{2} ET/)
  })

  it('drops y ticks that would stack into an unreadable corner', () => {
    // mostly-positive series: 0 and y_lo are 1 apart -> sub-14px apart
    const nearZeroFloor: HistorySeries = {
      points: [
        [0, -1],
        [1_000, 50],
        [2_000, 100],
      ],
      y_lo: -1,
      y_hi: 100,
      last: { ts_ms: 2_000, pnl: 100, pos: true },
    }
    const { container } = render(<TimeSeriesChart series={nearZeroFloor} ariaLabel="pnl" />)
    const yLabels = [...container.querySelectorAll('text.axis-label')]
      .map((t) => t.textContent)
      .filter((l) => !l?.includes('ET'))
    expect(yLabels).toEqual(['+$100', '+$0']) // the -$1 tick was dropped
    expect(container.querySelectorAll('.zero-line').length).toBe(1)
  })

  it('lifts the flipped end label clear of a steep final segment', () => {
    const steep: HistorySeries = {
      points: [
        [0, 1],
        [20_000, 2],
        [40_000, 3],
        [60_000, 90],
      ],
      y_lo: 1,
      y_hi: 90,
      last: { ts_ms: 60_000, pnl: 90, pos: true },
    }
    const { container } = render(<TimeSeriesChart series={steep} ariaLabel="pnl" />)
    const svg = container.querySelector('svg')!
    const vb = Number(svg.getAttribute('viewBox')!.split(' ')[2])
    const dotX = Number(
      svg.querySelector('circle.dot-ring')!.getAttribute('cx'),
    )
    const endText = [...svg.querySelectorAll('text.plateau-label')].find(
      (t) => Number(t.getAttribute('x') ?? 0) < Number(dotX),
    )
    expect(endText).toBeDefined()
    expect(endText!.getAttribute('x')).toBeDefined()
    // ascending into the dot -> label sits ABOVE it (smaller svg y)
    const dotY = Number(svg.querySelector('circle.dot-ring')!.getAttribute('cy'))
    const textY = Number(endText!.getAttribute('y') as string)
    expect(textY).toBeLessThan(dotY)
    expect(vb).toBeGreaterThan(0)
  })

  it('keeps every label inside the drawing area', () => {
    const { container } = render(
      <TimeSeriesChart series={equity} ariaLabel="equity" valueFormat={usdLevel} />,
    )
    const vw = viewBoxWidth(container.querySelector('svg')!)
    for (const t of container.querySelectorAll('text')) {
      const x = Number(t.getAttribute('x'))
      const w = labelWidth(t.textContent ?? '', t.classList.contains('plateau-label') ? 12.5 : 12)
      const anchor = t.getAttribute('text-anchor') ?? 'start'
      const left = anchor === 'end' ? x - w : x
      expect(left, t.textContent ?? '').toBeGreaterThanOrEqual(0)
      expect(left + w, t.textContent ?? '').toBeLessThanOrEqual(vw)
    }
  })

  it('draws at the measured width (a phone chart is not a scaled-down 640)', () => {
    let fire: (w: number) => void = () => undefined
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(cb: ResizeObserverCallback) {
          fire = (w) =>
            cb([{ contentRect: { width: w } } as ResizeObserverEntry], this as never)
        }
        observe() {}
        disconnect() {}
      },
    )
    const { container } = render(
      <TimeSeriesChart series={equity} ariaLabel="equity" valueFormat={usdLevel} />,
    )
    act(() => fire(336))
    expect(viewBoxWidth(container.querySelector('svg')!)).toBe(336)
  })
})
