import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DensityProvider, useDensity } from './density'
import { DensityToggle } from './components/DensityToggle'

function Probe() {
  const { mode } = useDensity()
  return <p data-testid="mode">{mode}</p>
}

describe('density', () => {
  it('defaults to operator and persists a switch to simple', () => {
    localStorage.clear()
    render(
      <DensityProvider>
        <Probe />
        <DensityToggle />
      </DensityProvider>,
    )
    expect(screen.getByTestId('mode').textContent).toBe('operator')

    fireEvent.click(screen.getByRole('button', { name: 'Simple' }))
    expect(screen.getByTestId('mode').textContent).toBe('simple')
    expect(localStorage.getItem('trex.density')).toBe('simple')
  })
})
