import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CONTROL_LABELS, type TodayView } from '../lib/contracts'
import { TodayPage } from './TodayPage'
import {
  fakeResponse,
  flush,
  installFetch,
  posts,
  resultFor,
  todayFixture,
  todayUnavailableFixture,
} from '../lib/reviewTestKit'

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(Date.UTC(2026, 9, 22, 14, 2, 0))
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

/** The fixture as a live server would answer it at the (fake) clock: `now`
 * advances and each approval's `seconds_to_deadline` is counted at it. */
function liveToday(): TodayView {
  const view = todayFixture()
  const nowMs = Math.floor(Date.now() / 1000) * 1000
  view.now = new Date(nowMs).toISOString().replace(/\.\d{3}Z$/, 'Z')
  for (const item of view.approvals.items!) {
    item.seconds_to_deadline = Math.max(0, Math.floor((Date.parse(item.send_deadline) - nowMs) / 1000))
  }
  return view
}

function serve(view: TodayView | (() => TodayView)) {
  return installFetch((call) => {
    if (call.url === 'api/today' && call.method === 'GET') {
      return fakeResponse(200, typeof view === 'function' ? view() : view)
    }
    if (call.url === 'api/commands' && call.method === 'POST') return fakeResponse(202, resultFor(call.body!, 'accepted'))
    return fakeResponse(404, { error: 'unknown' })
  })
}

async function mount(view: TodayView | (() => TodayView)) {
  const calls = serve(view)
  render(<TodayPage />)
  await flush()
  return calls
}

const region = (name: string) => screen.getByRole('region', { name })

describe('TodayPage with every block known', () => {
  it('shows status, approvals, positions, acceptance, blockers and next events from the view', async () => {
    const today = todayFixture()
    const calls = await mount(today)
    expect(calls[0]).toEqual({ url: 'api/today', method: 'GET', body: null })

    const status = region('Desk status')
    expect(status.textContent).toContain('Market open')
    expect(status.textContent).toContain('09:30')
    expect(status.textContent).toContain('16:00')
    expect(status.textContent).toContain('Exits are watched')
    expect(status.textContent).toContain('API answering')
    expect(status.textContent).toContain('New entries open')

    const approvals = region('Pending approvals')
    const item = today.approvals.items![0]
    expect(within(approvals).getByText(item.summary)).toBeTruthy()
    expect(within(approvals).getByRole('timer').textContent).toBe('8:00 left')
    expect(approvals.textContent).toContain('$268.60')
    expect(approvals.textContent).toContain('$1,343.00')
    const details = within(approvals).getByRole('link', { name: 'Full details' })
    expect(details.getAttribute('href')).toBe(`#/trade-review/${item.proposal_id}`)

    const positions = region('Open positions')
    expect(positions.textContent).toContain('1 option position')
    expect(positions.textContent).toContain('0 equity positions')
    expect(positions.textContent).toContain('$47.00')
    expect(positions.textContent).toContain('$25,000.00')

    const acceptance = region('Paper acceptance')
    expect(acceptance.textContent).toContain('2 of 10')
    expect(acceptance.textContent).toContain('1 of 10')
    expect(acceptance.textContent).toContain('0 of 10')
    expect(acceptance.textContent).toContain('1 of 7')
    expect(acceptance.textContent).toContain('Live trading is not authorized')

    const blockers = region('Blockers')
    expect(blockers.textContent).toContain(today.blockers[0].message)
    expect(blockers.textContent).toContain(today.blockers[0].next_step)
    expect(blockers.textContent).toContain(today.blockers[0].code)

    const events = region('Coming up')
    for (const event of today.next_events.items!) expect(events.textContent).toContain(event.label)

    const controls = region('Controls')
    expect(controls.textContent).toContain(CONTROL_LABELS.halt)
    expect(controls.textContent).toContain(CONTROL_LABELS.flatten)
    expect(controls.textContent).toContain('owner@example.com')
    expect(posts(calls)).toHaveLength(0)
  })

  it('the approval countdown runs from the response, re-aligns on refresh and reaches Expired', async () => {
    await mount(liveToday)
    await flush(5000)
    expect(within(region('Pending approvals')).getByRole('timer').textContent).toBe('7:55 left')
    await flush(10_000) // the 15 s re-poll answers 465 s at its own now
    expect(within(region('Pending approvals')).getByRole('timer').textContent).toBe('7:45 left')
    await flush(465_000)
    expect(within(region('Pending approvals')).getByRole('timer').textContent).toBe('Expired')
    const review = within(region('Pending approvals')).getByRole('button', { name: 'Review and approve' }) as HTMLButtonElement
    expect(review.disabled).toBe(true)
  })

  it('opening an approval sends nothing until the owner presses Approve', async () => {
    const today = todayFixture()
    const calls = await mount(today)
    fireEvent.click(within(region('Pending approvals')).getByRole('button', { name: 'Review and approve' }))
    const dialog = screen.getByRole('dialog')
    expect(posts(calls)).toHaveLength(0)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve entry' }))
    await flush()
    const sent = posts(calls)
    expect(sent).toHaveLength(1)
    expect(JSON.parse(sent[0].body!).args).toEqual(today.approvals.items![0].approve_args)
  })
})

describe('the pause is the primary control', () => {
  it('Pause new entries queues pause_entries and says exits keep running', async () => {
    const today = todayFixture()
    today.controls.queued = { status: 'ok', unavailable_reason: null, items: [] }
    const calls = await mount(today)
    const controls = region('Controls')
    expect(controls.textContent).toContain('Exits keep running')
    fireEvent.click(within(controls).getByRole('button', { name: CONTROL_LABELS.pause_entries }))
    await flush()
    const sent = posts(calls)
    expect(sent).toHaveLength(1)
    expect(JSON.parse(sent[0].body!).kind).toBe('pause_entries')
    expect(within(controls).queryByRole('button', { name: CONTROL_LABELS.halt })).toBeNull()
  })

  it('a queued pause waits for the desk owner instead of queueing another', async () => {
    await mount(todayFixture())
    const controls = region('Controls')
    const pause = within(controls).getByRole('button', { name: CONTROL_LABELS.pause_entries }) as HTMLButtonElement
    expect(pause.disabled).toBe(true)
    expect(controls.textContent).toContain('queued for the desk owner')
  })

  it('a standing pause offers Resume new entries', async () => {
    const today = todayFixture()
    today.controls.queued = { status: 'ok', unavailable_reason: null, items: [] }
    today.controls.pause_entries = {
      name: 'PAUSE_ENTRIES',
      status: 'standing',
      unavailable_reason: null,
      since: '2026-10-22T13:45:00Z',
      reason: 'owner break',
    }
    const calls = await mount(today)
    const controls = region('Controls')
    expect(within(controls).queryByRole('button', { name: CONTROL_LABELS.pause_entries })).toBeNull()
    expect(controls.textContent).toContain('owner break')
    expect(region('Desk status').textContent).toContain('New entries paused')
    fireEvent.click(within(controls).getByRole('button', { name: CONTROL_LABELS.resume_entries }))
    await flush()
    expect(JSON.parse(posts(calls)[0].body!).kind).toBe('resume_entries')
  })
})

describe('TodayPage with every block unavailable', () => {
  it('names each reason and never renders unknown as zero', async () => {
    const down = todayUnavailableFixture()
    await mount(down)
    const page = document.body.textContent ?? ''
    for (const reason of [
      down.session.unavailable_reason,
      down.desk_owner.unavailable_reason,
      down.gateway.unavailable_reason,
      down.controls.pause_entries.unavailable_reason,
      down.controls.queued.unavailable_reason,
      down.approvals.unavailable_reason,
      down.positions.holdings.unavailable_reason,
      down.positions.risk.unavailable_reason,
      down.acceptance.unavailable_reason,
      down.next_events.unavailable_reason,
    ]) {
      expect(page).toContain(reason!)
    }
    expect(page).not.toMatch(/\$0\.00|NaN|undefined|null/)
    expect(region('Open positions').textContent).not.toMatch(/\b0\b/)
    expect(region('Paper acceptance').textContent).not.toMatch(/\b0 of\b/)
    expect(screen.queryByText('Nothing is waiting for your approval.')).toBeNull()
    // pausing is always safe, resuming an unknown state is never offered
    const controls = region('Controls')
    expect(within(controls).getByRole('button', { name: CONTROL_LABELS.pause_entries })).toBeTruthy()
    expect(within(controls).queryByRole('button', { name: CONTROL_LABELS.resume_entries })).toBeNull()
    for (const blocker of down.blockers) expect(region('Blockers').textContent).toContain(blocker.message)
  })

  it('a failed load is an alert, never an empty desk', async () => {
    installFetch(() => fakeResponse(503, { error: 'down' }))
    render(<TodayPage />)
    await flush()
    expect(screen.getByRole('alert').textContent).toContain('503')
    expect(screen.queryByRole('region', { name: 'Pending approvals' })).toBeNull()
  })
})
