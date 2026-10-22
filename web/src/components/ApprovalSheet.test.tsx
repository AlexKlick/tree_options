import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApprovalSheet } from './ApprovalSheet'
import {
  fakeResponse,
  flush,
  installFetch,
  posts,
  resultFor,
  todayFixture,
  type FakeCall,
} from '../lib/reviewTestKit'

// The device clock is five hours off the desk's: every instant the sheet
// sends must come from the response's `now`, never from this clock.
const CLIENT_START = Date.UTC(2026, 9, 22, 9, 0, 0)

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(CLIENT_START)
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function mount(props: { closedReason?: string | null } = {}) {
  const today = todayFixture()
  const approval = today.approvals.items![0]
  const view = render(
    <ApprovalSheet
      approval={approval}
      receivedAtMs={Date.now()}
      responseNow={today.now}
      mode="dialog"
      pollMs={1000}
      closedReason={props.closedReason ?? null}
    />,
  )
  return { approval, view }
}

const approveButton = () => screen.getByRole('button', { name: 'Approve entry' }) as HTMLButtonElement
const rejectButton = () => screen.getByRole('button', { name: 'Reject' }) as HTMLButtonElement
const timer = () => screen.getByRole('timer').textContent

describe('ApprovalSheet', () => {
  it('counts down from the moment the response arrived and never sends by itself', async () => {
    const calls = installFetch(() => fakeResponse(500, {}))
    mount()
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(timer()).toBe('8:00 left')
    await flush(2000)
    expect(timer()).toBe('7:58 left')
    await flush(60_000)
    expect(timer()).toBe('6:58 left')
    expect(calls).toHaveLength(0)
  })

  it('Approve sends approve_entry with the approval args verbatim, issued on the desk clock', async () => {
    const calls = installFetch((call) => fakeResponse(202, resultFor(call.body!, 'accepted')))
    const { approval } = mount()
    await flush(3500)
    fireEvent.click(approveButton())
    await flush()
    const sent = posts(calls)
    expect(sent).toHaveLength(1)
    expect(sent[0].url).toBe('api/commands')
    const body = JSON.parse(sent[0].body!)
    expect(body.kind).toBe('approve_entry')
    expect(body.args).toEqual(approval.approve_args)
    expect(body.issued_at).toBe('2026-10-22T14:02:03Z')
    expect(body.expires_at).toBe('2026-10-22T14:07:03Z')
    expect(screen.getByRole('status').textContent).toContain('Queued for the desk owner')
  })

  it('the command never outlives the approval window', async () => {
    const calls = installFetch((call) => fakeResponse(202, resultFor(call.body!, 'accepted')))
    mount()
    await flush(470_000)
    expect(timer()).toBe('0:10 left')
    fireEvent.click(approveButton())
    await flush()
    const body = JSON.parse(posts(calls)[0].body!)
    expect(Date.parse(body.expires_at) - Date.parse(body.issued_at)).toBe(10_000)
  })

  it('at expiry the decision buttons are disabled and nothing is sent', async () => {
    const calls = installFetch(() => fakeResponse(500, {}))
    mount()
    await flush(480_000)
    expect(timer()).toBe('Expired')
    expect(approveButton().disabled).toBe(true)
    expect(rejectButton().disabled).toBe(true)
    fireEvent.click(approveButton())
    await flush()
    expect(calls).toHaveLength(0)
  })

  it('polls GET api/commands/{key} and reports the desk owner applying the approval', async () => {
    let calls: FakeCall[] = []
    calls = installFetch((call) => {
      if (call.method === 'POST') return fakeResponse(202, resultFor(call.body!, 'accepted'))
      const body = posts(calls)[0].body!
      return fakeResponse(
        200,
        resultFor(body, 'applied', { result: { expires_at: '2026-10-22T14:07:00Z', mandate: 'mandates/eq-spy-001.json' } }),
      )
    })
    mount()
    fireEvent.click(approveButton())
    await flush()
    await flush(1000)
    const key = JSON.parse(posts(calls)[0].body!).idempotency_key
    expect(calls.filter((c) => c.method === 'GET').map((c) => c.url)).toEqual([`api/commands/${key}`])
    const status = screen.getByRole('status').textContent ?? ''
    expect(status).toContain('Approved')
    expect(status).toContain('10:07')
  })

  it('a retry after a network drop re-sends the same request under the same key', async () => {
    let drop = true
    const calls = installFetch((call) => {
      if (drop) {
        drop = false
        throw new TypeError('Failed to fetch')
      }
      return fakeResponse(202, resultFor(call.body!, 'accepted'))
    })
    mount()
    fireEvent.click(approveButton())
    await flush()
    expect(screen.getByRole('status').textContent).toContain('Not confirmed')
    expect(approveButton().disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Try again (same request)' }))
    await flush()
    const sent = posts(calls)
    expect(sent).toHaveLength(2)
    expect(sent[1].body).toBe(sent[0].body)
    expect(screen.getByRole('status').textContent).toContain('Queued for the desk owner')
  })

  it('a desk refusal shows the vocabulary reason and its code', async () => {
    let calls: FakeCall[] = []
    calls = installFetch((call) => {
      if (call.method === 'POST') return fakeResponse(202, resultFor(call.body!, 'accepted'))
      return fakeResponse(
        200,
        resultFor(posts(calls)[0].body!, 'refused', {
          reason: 'approval_epoch_mismatch',
          result: { blockers: ['approval_epoch_mismatch', 'entries_paused'] },
        }),
      )
    })
    mount()
    fireEvent.click(approveButton())
    await flush()
    await flush(1000)
    const status = screen.getByRole('status')
    expect(status.textContent).toContain('Refused')
    expect(status.textContent).toContain('approval_epoch_mismatch')
    expect(status.textContent).toContain('entries_paused')
    expect(status.textContent).not.toContain('approval epoch mismatch')
  })

  it('Reject asks for an optional reason and sends reject_entry for this proposal', async () => {
    const calls = installFetch((call) => fakeResponse(202, resultFor(call.body!, 'accepted')))
    const { approval } = mount()
    fireEvent.click(rejectButton())
    expect(posts(calls)).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Why (optional)'), { target: { value: '  spread too wide  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm rejection' }))
    await flush()
    const body = JSON.parse(posts(calls)[0].body!)
    expect(body.kind).toBe('reject_entry')
    expect(body.args).toEqual({ proposal_id: approval.proposal_id, reason: 'spread too wide' })
  })

  it('a closed proposal disables the decision and says why', () => {
    installFetch(() => fakeResponse(500, {}))
    mount({ closedReason: 'This proposal is no longer waiting for approval.' })
    expect(approveButton().disabled).toBe(true)
    expect(rejectButton().disabled).toBe(true)
    expect(screen.getByText('This proposal is no longer waiting for approval.')).toBeTruthy()
  })
})
