import { render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import * as api from '../lib/activityApi'
import type { TodayView } from '../lib/contracts'
import { OnboardingPage } from './OnboardingPage'

vi.mock('../lib/activityApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/activityApi')>()),
  getToday: vi.fn(),
}))
afterEach(() => vi.resetAllMocks())

const wave3 = import.meta.glob('../../../tests/contracts/fixtures/wave3/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>
function fixture<T>(name: string): T {
  return structuredClone(wave3[`../../../tests/contracts/fixtures/wave3/${name}`]) as T
}

const TITLES = [
  'IB Gateway logged in',
  'Desk owner heartbeat',
  'Capital profile present',
  'Family command key installed',
  'First paper approval',
]

it('walks the five first-run steps from /api/today', async () => {
  vi.mocked(api.getToday).mockResolvedValue(fixture<TodayView>('today.json'))
  render(<OnboardingPage />)
  const list = await screen.findByRole('list', { name: 'First-run checklist' })
  const steps = within(list).getAllByRole('listitem')
  expect(steps.map((s) => within(s).getByRole('heading').textContent)).toEqual(TITLES)
  for (const step of steps) expect(within(step).getByText('Done')).toBeTruthy()
  expect(screen.getByText('5 of 5 steps done')).toBeTruthy()
})

it('counts unknown steps apart from done ones and says why', async () => {
  vi.mocked(api.getToday).mockResolvedValue(fixture<TodayView>('today_unavailable.json'))
  render(<OnboardingPage />)
  const list = await screen.findByRole('list', { name: 'First-run checklist' })
  expect(screen.getByText('1 of 5 steps done, 4 unknown')).toBeTruthy()
  const gateway = within(list).getAllByRole('listitem')[0]
  expect(within(gateway).getByText('Unknown')).toBeTruthy()
  expect(within(gateway).getByText(/gateway_watch_not_reporting/)).toBeTruthy()
})

it('links a waiting proposal to its review page', async () => {
  const today = fixture<TodayView>('today.json')
  vi.mocked(api.getToday).mockResolvedValue({
    ...today,
    acceptance: { ...today.acceptance, option_lifecycles: 0, equity_lifecycles: 0 } as TodayView['acceptance'],
    positions: {
      ...today.positions,
      holdings: { ...today.positions.holdings, option_positions: 0, equity_positions: 0 } as TodayView['positions']['holdings'],
    },
  })
  render(<OnboardingPage />)
  const list = await screen.findByRole('list', { name: 'First-run checklist' })
  const approval = within(list).getAllByRole('listitem')[4]
  expect(within(approval).getByText('To do')).toBeTruthy()
  expect(within(approval).getByRole('link').getAttribute('href')).toBe(`#/trade-review/${today.approvals.items![0].proposal_id}`)
  expect(screen.getByText('4 of 5 steps done')).toBeTruthy()
})

it('marks every step unknown when Today cannot be read', async () => {
  vi.mocked(api.getToday).mockRejectedValue(new Error('fetch api/today failed: 503'))
  render(<OnboardingPage />)
  expect((await screen.findByRole('alert')).textContent).toContain('503')
  const list = screen.getByRole('list', { name: 'First-run checklist' })
  expect(within(list).getAllByText('Unknown')).toHaveLength(5)
  expect(screen.getByText('0 of 5 steps done, 5 unknown')).toBeTruthy()
})
