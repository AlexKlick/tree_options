import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProposalList, TodayView } from '../lib/contracts'
import { TradeReviewPage } from './TradeReviewPage'
import {
  fakeResponse,
  flush,
  installFetch,
  optionRow,
  posts,
  proposalListFixture,
  resultFor,
  todayFixture,
} from '../lib/reviewTestKit'

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(Date.UTC(2026, 9, 22, 14, 2, 0))
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function serve(list: ProposalList, today: TodayView | null = todayFixture()) {
  return installFetch((call) => {
    if (call.method === 'GET' && call.url === 'api/proposals') return fakeResponse(200, list)
    if (call.method === 'GET' && call.url === 'api/today') {
      return today ? fakeResponse(200, today) : fakeResponse(503, { error: 'down' })
    }
    if (call.method === 'POST' && call.url === 'api/commands') return fakeResponse(202, resultFor(call.body!, 'accepted'))
    return fakeResponse(404, { error: 'unknown' })
  })
}

async function mount(list: ProposalList, proposalId: string | null, today?: TodayView | null) {
  const calls = serve(list, today)
  render(<TradeReviewPage proposalId={proposalId} />)
  await flush()
  return calls
}

const facts = () => screen.getByRole('region', { name: 'Proposal facts' })

describe('Trade review index', () => {
  it('lists pending proposals with a live countdown and a link to each', async () => {
    const list = proposalListFixture()
    const row = list.proposals[0]
    const calls = await mount(list, null)
    expect(calls.map((c) => c.url)).toContain('api/proposals')
    const pending = screen.getByRole('region', { name: 'Waiting for your decision' })
    expect(pending.textContent).toContain('Buy 2 SPY')
    expect(within(pending).getByRole('timer').textContent).toBe('8:00 left')
    await flush(3000)
    expect(within(pending).getByRole('timer').textContent).toBe('7:57 left')
    const link = within(pending).getByRole('link', { name: /Buy 2 SPY/ })
    expect(link.getAttribute('href')).toBe(`#/trade-review/${row.proposal_id}`)
    expect(posts(calls)).toHaveLength(0)
  })

  it('shows unreadable proposal files with their reason instead of dropping them', async () => {
    const list = proposalListFixture()
    list.problems = [{ file: 'broken.json', reason: 'proposal_invalid' }]
    list.proposals[0].status = 'expired'
    await mount(list, null)
    const problems = screen.getByRole('region', { name: 'Unreadable proposals' })
    expect(problems.textContent).toContain('broken.json')
    expect(problems.textContent).toContain('proposal_invalid')
    expect(screen.getByRole('region', { name: 'Earlier proposals' }).textContent).toContain('Expired')
  })
})

describe('Trade review detail (equity)', () => {
  it('shows every fact the owner approves', async () => {
    const list = proposalListFixture()
    const row = list.proposals[0]
    await mount(list, row.proposal_id)
    const text = facts().textContent ?? ''
    for (const expected of [
      'Buy 2 SPY',
      '$671.50',
      '$665.00',
      '3 quote ticks',
      '$680.00',
      '2026-10-23',
      '15:45 ET',
      '$268.60',
      '$1,343.00',
      '$671.48',
      '$671.52',
      'realtime',
      '2m old',
      row.proposal.review.screening_sha256,
      row.proposal.review.owner_epoch,
      row.proposal.review.profile_digest,
      row.proposal.effect_sha256,
      row.proposal_sha256,
      row.proposal.program_id,
      row.proposal.strategy_version,
      'sleeve-equity-01',
      'DU0000001',
      'No advisory pre-check flags',
    ]) {
      expect(text, expected).toContain(expected)
    }
    expect(screen.getByRole('timer').textContent).toBe('8:00 left')
  })

  it('Approve sends the same args the Today view carries for this proposal', async () => {
    const list = proposalListFixture()
    const today = todayFixture()
    const calls = await mount(list, list.proposals[0].proposal_id, today)
    fireEvent.click(screen.getByRole('button', { name: 'Approve entry' }))
    await flush()
    const sent = posts(calls)
    expect(sent).toHaveLength(1)
    const body = JSON.parse(sent[0].body!)
    expect(body.kind).toBe('approve_entry')
    expect(body.args).toEqual(today.approvals.items![0].approve_args)
    expect(body.issued_at).toBe('2026-10-22T14:02:00Z')
  })

  it('advisory pre-checks name the Today flags, or say they are unavailable', async () => {
    const list = proposalListFixture()
    const today = todayFixture()
    today.approvals.items![0].precheck_blockers = ['entries_paused']
    await mount(list, list.proposals[0].proposal_id, today)
    expect(facts().textContent).toContain('entries_paused')
  })

  it('a quote dated after the list time says so instead of claiming it is fresh', async () => {
    const list = proposalListFixture()
    list.proposals[0].proposal.review.quote.as_of = '2026-10-22T14:03:00Z'
    await mount(list, list.proposals[0].proposal_id)
    expect(facts().textContent).toContain('dated after the list time')
    expect(facts().textContent).not.toContain('0s old')
  })

  it('a Today outage leaves the pre-checks unavailable, not empty', async () => {
    const list = proposalListFixture()
    await mount(list, list.proposals[0].proposal_id, null)
    expect(facts().textContent).toContain('Advisory pre-checks unavailable')
    expect(facts().textContent).not.toContain('No advisory pre-check flags')
  })

  it('a decided proposal offers no decision', async () => {
    const list = proposalListFixture()
    list.proposals[0].status = 'approved'
    await mount(list, list.proposals[0].proposal_id)
    expect(screen.queryByRole('button', { name: 'Approve entry' })).toBeNull()
    expect(screen.getAllByText(/Approved/).length).toBeGreaterThan(0)
  })

  it('an unknown proposal id says it is not in the list and offers no decision', async () => {
    const list = proposalListFixture()
    await mount(list, 'f'.repeat(64))
    expect(screen.getByText(/is not in the desk's proposal list/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Approve entry' })).toBeNull()
    expect(screen.getByRole('link', { name: 'All proposals' }).getAttribute('href')).toBe('#/trade-review')
  })
})

describe('Trade review detail (option and unknown effects)', () => {
  it('shows the legs, the limit and the exit rules of an option structure', async () => {
    const list = proposalListFixture()
    const row = optionRow()
    list.proposals = [row]
    await mount(list, row.proposal_id)
    const text = facts().textContent ?? ''
    for (const expected of ['Buy 1 SPY debit vertical', '0.48', '760', '758', '2026-11-20', 'Put', 'gain_frac 0.5', 'None set', '2026-10-15', '$48.00', '$0.44', '$0.52']) {
      expect(text, expected).toContain(expected)
    }
    expect(screen.getByRole('table', { name: 'Legs' })).toBeTruthy()
  })

  it('an unrecognized effect schema is shown as recorded, never guessed', async () => {
    const list = proposalListFixture()
    const row = list.proposals[0]
    row.proposal.effect = { schema: 'future-effect/9', intent_id: 'x-1', widget: 7 }
    await mount(list, row.proposal_id)
    const text = facts().textContent ?? ''
    expect(text).toContain('future-effect/9')
    expect(text).toContain('"widget": 7')
  })
})
