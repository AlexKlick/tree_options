import { render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import * as api from '../lib/activityApi'
import { reasonText } from '../lib/activityApi'
import { ACCEPTANCE_THRESHOLDS, CONTROL_LABELS, COMMAND_MAX_TTL_S, PRIMARY_CONTROL, type TodayView } from '../lib/contracts'
import * as portfolio from '../lib/portfolioApi'
import { samplePortfolio } from '../lib/portfolioSample'
import { SettingsPanel } from './SettingsPanel'

vi.mock('../lib/activityApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/activityApi')>()),
  getToday: vi.fn(),
}))
vi.mock('../lib/portfolioApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/portfolioApi')>()),
  getPortfolio: vi.fn(),
}))
afterEach(() => vi.resetAllMocks())

const wave3 = import.meta.glob('../../../tests/contracts/fixtures/wave3/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>
function fixture<T>(name: string): T {
  return structuredClone(wave3[`../../../tests/contracts/fixtures/wave3/${name}`]) as T
}

it('explains every control with its exact label, the pause first, and duplicates none', async () => {
  vi.mocked(api.getToday).mockResolvedValue(fixture<TodayView>('today.json'))
  vi.mocked(portfolio.getPortfolio).mockResolvedValue(samplePortfolio())
  render(<SettingsPanel />)
  const controls = await screen.findByRole('region', { name: 'Controls' })
  const terms = within(controls).getAllByRole('term').map((t) => t.textContent)
  expect(terms).toEqual([
    CONTROL_LABELS[PRIMARY_CONTROL],
    CONTROL_LABELS.resume_entries,
    CONTROL_LABELS.halt,
    CONTROL_LABELS.flatten,
    CONTROL_LABELS.resume_all,
  ])
  expect(within(controls).getByText(/Primary control/)).toBeTruthy()
  expect(within(controls).getByText(/never stops exits/)).toBeTruthy()
  // explained, not duplicated: no button carries a control's label
  const labels = new Set<string>(Object.values(CONTROL_LABELS))
  for (const button of screen.queryAllByRole('button')) {
    expect(labels.has(button.textContent ?? '')).toBe(false)
  }
  expect(within(controls).getByRole('link', { name: /Today/ }).getAttribute('href')).toBe('#/today')
  const state = screen.getByRole('region', { name: 'Current state' })
  expect(within(state).getAllByText('Clear')).toHaveLength(3)
  expect(within(state).getByText(/1 command queued/)).toBeTruthy()
})

it('shows the read-only profile facts and leaves unknown limits unavailable', async () => {
  const today = fixture<TodayView>('today.json')
  vi.mocked(api.getToday).mockResolvedValue(today)
  vi.mocked(portfolio.getPortfolio).mockResolvedValue(samplePortfolio())
  render(<SettingsPanel />)
  const profile = await screen.findByRole('region', { name: 'Profile' })
  expect(within(profile).getByText('DU0000001')).toBeTruthy()
  expect(within(profile).getByText(today.approvals.items![0].approve_args.profile_digest.slice(0, 12), { exact: false })).toBeTruthy()
  expect(within(profile).getByText(/Loss limits/)).toBeTruthy()
  expect(within(profile).getByText(/Unavailable/)).toBeTruthy()
  expect(within(profile).getByText(`${today.desk_owner.stale_after_s} s`, { exact: false })).toBeTruthy()
  expect(within(profile).getByText(`${COMMAND_MAX_TTL_S} s`, { exact: false })).toBeTruthy()
  expect(within(profile).getByText(/Live trading/)).toBeTruthy()
  expect(within(profile).getByText(/Off\. Paper only/)).toBeTruthy()
  const acceptance = screen.getByRole('region', { name: 'Acceptance rules' })
  expect(within(acceptance).getByText(`${ACCEPTANCE_THRESHOLDS.streak_sessions} consecutive qualifying sessions`, { exact: false })).toBeTruthy()
  expect(within(acceptance).getByText(`${ACCEPTANCE_THRESHOLDS.heartbeat_min_percent}%`, { exact: false })).toBeTruthy()
})

it('says why a control state is unknown instead of showing it clear', async () => {
  vi.mocked(api.getToday).mockResolvedValue(fixture<TodayView>('today_unavailable.json'))
  vi.mocked(portfolio.getPortfolio).mockRejectedValue(new Error('fetch api/portfolio failed: 502'))
  render(<SettingsPanel />)
  const state = await screen.findByRole('region', { name: 'Current state' })
  expect(within(state).queryByText('Clear')).toBeNull()
  expect(within(state).getAllByText(reasonText('kill_file_unreadable'), { exact: false })).toHaveLength(3)
  expect(within(state).getByText(reasonText('command_store_unavailable'), { exact: false })).toBeTruthy()
  const profile = screen.getByRole('region', { name: 'Profile' })
  expect(within(profile).getByText(/502/)).toBeTruthy()
})

it('reports a failed Today read', async () => {
  vi.mocked(api.getToday).mockRejectedValue(new Error('fetch api/today failed: 503'))
  vi.mocked(portfolio.getPortfolio).mockResolvedValue(samplePortfolio())
  render(<SettingsPanel />)
  expect((await screen.findAllByRole('alert')).map((a) => a.textContent).join(' ')).toContain('503')
  // the explanations stay readable without live data
  expect(screen.getByRole('region', { name: 'Controls' })).toBeTruthy()
})
