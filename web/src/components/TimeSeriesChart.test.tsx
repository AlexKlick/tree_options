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
