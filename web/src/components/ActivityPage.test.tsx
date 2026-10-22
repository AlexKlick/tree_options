import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '../lib/activityApi'
import { ACTIVITY_PAGE_SIZE, COMMAND_STATUS_LABELS, DRILL_LABELS, INGRESS_LABELS, reasonText } from '../lib/activityApi'
import type { AcceptanceView, ActivityPage as ActivityPageView, DrillId, TodayView } from '../lib/contracts'
import * as portfolio from '../lib/portfolioApi'
import { samplePortfolio } from '../lib/portfolioSample'
import { ActivityPage } from './ActivityPage'

vi.mock('../lib/activityApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/activityApi')>()),
  getActivity: vi.fn(),
  getAcceptance: vi.fn(),
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

const lastPage = (first: ActivityPageView): ActivityPageView => ({
  ...first,
  has_more: false,
  next_cursor: null,
  items: [
    {
      id: 'desk_event:events.jsonl:1001',
      at: '2026-10-22T13:00:00Z',
      source: 'desk_event',
      kind: 'reconciliation_clean',
      summary: 'Books match the broker',
      actor: { sub: 'trex-desk', ingress: 'system' },
      actor_unavailable_reason: null,
      command_key: null,
      command_status: null,
      reason: null,
      refs: {},
    },
  ],
})

describe('activity feed', () => {
  it('shows who did what, through which door, with refused reasons and linked proposals', async () => {
    const page = fixture<ActivityPageView>('activity.json')
    vi.mocked(api.getActivity).mockResolvedValue(page)
    render(<ActivityPage />)
    await screen.findByText('Paused new entries (from the phone)')
    expect(api.getActivity).toHaveBeenCalledWith({ limit: ACTIVITY_PAGE_SIZE, cursor: null, sources: [] })
    const items = screen.getAllByRole('article')
    expect(items).toHaveLength(page.items.length)
    expect(within(items[0]).getByText(COMMAND_STATUS_LABELS.applied)).toBeTruthy()
    expect(within(items[0]).getByText(`owner@example.com via ${INGRESS_LABELS.family}`)).toBeTruthy()
    expect(within(items[2]).getByText(COMMAND_STATUS_LABELS.refused)).toBeTruthy()
    expect(within(items[2]).getByText(reasonText('approval_epoch_mismatch'), { exact: false })).toBeTruthy()
    const proposal = page.items[2].refs.proposal_id!
    expect(within(items[2]).getByRole('link', { name: /proposal/i }).getAttribute('href')).toBe(`#/trade-review/${proposal}`)
    expect(within(items[3]).getByText(/Actor not recorded/)).toBeTruthy()
    // the unreadable drill source is said out loud, not shown as an empty feed
    const sources = screen.getByRole('region', { name: 'Sources' })
    expect(within(sources).getByText(reasonText('activity_source_unreadable'), { exact: false })).toBeTruthy()
  })

  it('loads older pages with the cursor and stops at the end', async () => {
    const page = fixture<ActivityPageView>('activity.json')
    vi.mocked(api.getActivity).mockResolvedValueOnce(page).mockResolvedValueOnce(lastPage(page))
    render(<ActivityPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load older activity' }))
    await screen.findByText('Books match the broker')
    expect(api.getActivity).toHaveBeenLastCalledWith({ limit: ACTIVITY_PAGE_SIZE, cursor: page.next_cursor, sources: [] })
    expect(screen.getAllByRole('article')).toHaveLength(page.items.length + 1)
    expect(screen.queryByRole('button', { name: 'Load older activity' })).toBeNull()
    expect(screen.getByText(/Start of the activity log/)).toBeTruthy()
  })

  it('filters by source on the server and starts again from the newest page', async () => {
    const page = fixture<ActivityPageView>('activity.json')
    const commands = { ...lastPage(page), items: [page.items[0], page.items[2]] }
    vi.mocked(api.getActivity).mockResolvedValueOnce(page).mockResolvedValueOnce(commands)
    render(<ActivityPage />)
    await screen.findByText('HALT removed at the terminal')
    fireEvent.click(screen.getByRole('button', { name: 'Commands' }))
    expect(api.getActivity).toHaveBeenLastCalledWith({ limit: ACTIVITY_PAGE_SIZE, cursor: null, sources: ['command'] })
    await waitFor(() => expect(screen.queryAllByRole('article')).toHaveLength(2))
    expect(screen.queryByText('HALT removed at the terminal')).toBeNull()
    expect(screen.getByRole('button', { name: 'Commands' }).getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByRole('button', { name: 'All sources' }).getAttribute('aria-pressed')).toBe('false')
  })

  it('shows a failed read as an error, never as an empty log', async () => {
    vi.mocked(api.getActivity).mockRejectedValue(new Error('fetch api/activity failed: 503'))
    render(<ActivityPage />)
    expect((await screen.findByRole('alert')).textContent).toContain('503')
    expect(screen.queryByText(/No activity recorded/)).toBeNull()
  })

  it('links the three sections of Activity & settings', async () => {
    vi.mocked(api.getActivity).mockResolvedValue(fixture<ActivityPageView>('activity.json'))
    render(<ActivityPage />)
    const nav = await screen.findByRole('navigation', { name: 'Activity & settings' })
    const links = within(nav).getAllByRole('link')
    expect(links.map((a) => a.getAttribute('href'))).toEqual(['#/activity', '#/activity/acceptance', '#/activity/settings'])
    expect(links[0].getAttribute('aria-current')).toBe('page')
  })
})

describe('paper acceptance', () => {
  it('shows progress against every requirement and keeps live trading off', async () => {
    const view = fixture<AcceptanceView>('acceptance_view.json')
    vi.mocked(api.getAcceptance).mockResolvedValue(view)
    render(<ActivityPage section="acceptance" />)
    const summary = await screen.findByRole('region', { name: 'Progress' })
    expect(within(summary).getByText('2 of 10')).toBeTruthy()
    expect(within(summary).getByText('1 of 10')).toBeTruthy()
    expect(within(summary).getByText('0 of 10')).toBeTruthy()
    expect(within(summary).getByText('1 of 7')).toBeTruthy()
    expect(within(summary).getByText(/Not met yet/)).toBeTruthy()
    expect(screen.getAllByText(/never unlocks live trading/).length).toBeGreaterThan(0)
    const drills = screen.getByRole('region', { name: 'Drills' })
    expect(within(drills).getByText(DRILL_LABELS.notification)).toBeTruthy()
    expect(within(drills).getByText(/Alert arrived after 9 minutes/)).toBeTruthy()
    const sessions = screen.getByRole('region', { name: 'Sessions' })
    expect(within(sessions).getByText(reasonText('heartbeat_below_99pct'), { exact: false })).toBeTruthy()
    const lifecycles = screen.getByRole('region', { name: 'Paper trades' })
    expect(within(lifecycles).getByText(reasonText('not_exited'), { exact: false })).toBeTruthy()
  })

  it('takes each drill pill from the computed result, never from the recorded status', async () => {
    const view = fixture<AcceptanceView>('acceptance_view.json')
    if (view.status !== 'ok') throw new Error('the acceptance_view fixture must be readable')
    const doc = view.document
    // Recorded "passed" but attested by someone who is not an owner: the
    // server computes it not passed (acceptance.py _drill_reasons).
    doc.drills = doc.drills.map((d) => (d.id === 'relogin' ? { ...d, status: 'passed', attested_by: 'visitor@example.com' } : d))
    doc.drill_results = doc.drill_results.map((r) =>
      r.id === 'relogin' ? { ...r, passed: false, reasons: ['attester_not_owner'] } : r,
    )
    doc.drills_passed = 0
    vi.mocked(api.getAcceptance).mockResolvedValue(view)
    render(<ActivityPage section="acceptance" />)
    const drills = await screen.findByRole('region', { name: 'Drills' })
    const row = (id: DrillId) => within(within(drills).getByText(DRILL_LABELS[id]).closest('li')!)
    expect(within(drills).queryByText('Passed')).toBeNull()
    expect(row('relogin').getByText('Not accepted').className).toContain('acct-status-bad')
    expect(row('relogin').getByText(reasonText('attester_not_owner'))).toBeTruthy()
    expect(row('notification').getByText('Failed').className).toContain('acct-status-bad')
    expect(row('backup_restore').getByText('Not run').className).toContain('acct-status-unknown')
    expect(within(screen.getByRole('region', { name: 'Progress' })).getByText('0 of 7')).toBeTruthy()
  })

  it('shows a computed pass as passed', async () => {
    const view = fixture<AcceptanceView>('acceptance_view.json')
    vi.mocked(api.getAcceptance).mockResolvedValue(view)
    render(<ActivityPage section="acceptance" />)
    const drills = await screen.findByRole('region', { name: 'Drills' })
    const relogin = within(within(drills).getByText(DRILL_LABELS.relogin).closest('li')!)
    expect(relogin.getByText('Passed').className).toContain('acct-status-ok')
    expect(within(drills).getAllByText('Passed')).toHaveLength(1)
  })

  it('shows a missing acceptance record as unavailable, with no counts', async () => {
    vi.mocked(api.getAcceptance).mockResolvedValue({
      schema: 'trex-acceptance-view/1',
      now: '2026-10-22T14:00:00Z',
      status: 'unavailable',
      unavailable_reason: 'acceptance_missing',
      document: null,
      live_authorized: false,
    })
    render(<ActivityPage section="acceptance" />)
    expect(await screen.findByText(reasonText('acceptance_missing'), { exact: false })).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/\d+ of (10|7)\b/)
    expect(screen.getAllByText(/never unlocks live trading/).length).toBeGreaterThan(0)
  })
})

describe('settings section', () => {
  it('renders the settings panel under Activity & settings', async () => {
    vi.mocked(api.getToday).mockResolvedValue(fixture<TodayView>('today.json'))
    vi.mocked(portfolio.getPortfolio).mockResolvedValue(samplePortfolio())
    render(<ActivityPage section="settings" />)
    expect(await screen.findByRole('region', { name: 'Controls' })).toBeTruthy()
    const nav = screen.getByRole('navigation', { name: 'Activity & settings' })
    expect(within(nav).getByRole('link', { name: 'Settings' }).getAttribute('aria-current')).toBe('page')
  })
})
