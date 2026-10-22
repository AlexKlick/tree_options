import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { getPlan } from '../lib/api'
import { PlanDetail } from './PlanDetail'

// getGateway/getExitMachine feed AppShell's health banners; never settle here
vi.mock('../lib/api', () => ({
  getPlan: vi.fn(),
  getGateway: () => new Promise(() => {}),
  getExitMachine: () => new Promise(() => {}),
}))
const mockedGetPlan = vi.mocked(getPlan)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('PlanDetail', () => {
  it('links back to the Plans list, never to #/ (Today since Wave 3)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 404 })))
    mockedGetPlan.mockRejectedValue(new Error('HTTP 404'))
    render(<PlanDetail id="missing-plan" />)
    const back = await screen.findByRole('link', { name: /All plans/ })
    expect(back.getAttribute('href')).toBe('#/plans')
  })
})
