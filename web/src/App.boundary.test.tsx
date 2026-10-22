import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

// A page that meets a view shape it does not expect throws while rendering
// (e.g. formatCents on a null). The app keeps the shell and its control bar.
vi.mock('./components/TodayPage', () => ({
  TodayPage: () => {
    throw new Error('formatCents: not a number')
  },
}))

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 404 })))
  vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  history.replaceState(null, '', '#')
})

describe('a page that fails to render', () => {
  it('shows an alert inside the shell; the control bar stays reachable', () => {
    history.replaceState(null, '', '#/today')
    render(<App />)
    expect(screen.getByRole('heading', { level: 1, name: 'This page could not be shown' })).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('formatCents: not a number')
    expect(screen.getByRole('region', { name: 'Desk controls' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Pause new entries' })).toBeTruthy()
  })
})
