import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PayoffChart } from './PayoffChart'
import { interpAt } from '../lib/interp'
import { usdSigned } from '../lib/format'
import type { Payoff } from '../lib/types'

const payoff: Payoff = {
  structure_id: 'nvda-oct',
  underlying: 'NVDA',
  view: { x_lo: 100, x_hi: 250 },
  points: [
    [100, 5000],
    [150, 5000],
    [170.21, 0],
    [250, -1050],
  ],
  levels: {
    long_strike: 185,
    short_strike: 150,
    entry: 0.21,
    qty: 5,
    width: 35,
    breakeven: 170.21,
    max_gain: 5000,
    max_loss: -1050,
    spot: null,
  },
  labels: { max_gain: '+$5,000', max_loss: '-$1,050' },
}

const pinWith = (want: string) =>
  screen.getByText(
    (_, el) => el?.classList.contains('chart-pin') === true && el.textContent === want,
  )

describe('PayoffChart what-if slider', () => {
  it('nudges by $0.50 with arrow keys and pins the interpolated pnl', () => {
    render(<PayoffChart payoff={payoff} />)
    const slider = screen.getByRole('slider')
    // no spot -> defaults to mid-range
    expect(slider.getAttribute('aria-valuenow')).toBe('175')

    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    expect(slider.getAttribute('aria-valuenow')).toBe('175.5')
    const want = `$175.50 → ${usdSigned(interpAt(payoff.points, 175.5))}`
    expect(pinWith(want)).toBeTruthy()

    // Shift = big step
    fireEvent.keyDown(slider, { key: 'ArrowRight', shiftKey: true })
    expect(slider.getAttribute('aria-valuenow')).toBe('180.5')

    // Escape unpins -> back to the default, pin gone
    fireEvent.keyDown(slider, { key: 'Escape' })
    expect(slider.getAttribute('aria-valuenow')).toBe('175')
    expect(
      screen.queryByText((_, el) => el?.classList.contains('chart-pin') === true),
    ).toBeNull()
  })

  it('clamps keyboard travel to the view and answers Home/End', () => {
    render(<PayoffChart payoff={payoff} />)
    const slider = screen.getByRole('slider')
    fireEvent.keyDown(slider, { key: 'End' })
    expect(slider.getAttribute('aria-valuenow')).toBe('250')
    fireEvent.keyDown(slider, { key: 'ArrowRight' })
    expect(slider.getAttribute('aria-valuenow')).toBe('250')
    fireEvent.keyDown(slider, { key: 'Home' })
    expect(slider.getAttribute('aria-valuenow')).toBe('100')
    fireEvent.keyDown(slider, { key: 'ArrowLeft' })
    expect(slider.getAttribute('aria-valuenow')).toBe('100')
  })
})
