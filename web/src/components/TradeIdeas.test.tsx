import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CommandResult, EntryProposalDoc } from '../lib/contracts'
import { TradeReviewPage } from './TradeReviewPage'
import {
  fakeResponse,
  flush,
  installFetch,
  posts,
  proposalListFixture,
  resultFor,
  todayFixture,
  type FakeCall,
} from '../lib/reviewTestKit'

// Every expectation derives from the frozen Wave 2 fixtures (the option and
// equity proposals, the equity effect) or from the idea list built here from
// them in the `trex-idea-list/1` shape (contracts.ts; the shared Wave 5
// fixtures themselves are read through the parser in ideasApi.test.ts).

const wave2 = import.meta.glob('../../../tests/contracts/fixtures/*.json', { eager: true, import: 'default' })
function fixture<T>(name: string): T {
  const hit = Object.entries(wave2).find(([path]) => path.endsWith(`/${name}`))
  if (!hit) throw new Error(`fixture ${name} not found`)
  return JSON.parse(JSON.stringify(hit[1])) as T
}

interface EquityEffect {
  symbol: string
  quantity: number
  limit_cents: number
  exit_policy: { stop_price_cents: number; target_price_cents: number | null; exit_deadline_session: string }
}
const optionProposal = () => fixture<EntryProposalDoc>('proposal_option.json')
const equityEffect = () => fixture<EquityEffect>('equity_effect.json')
const dollars = (cents: number): string => (cents / 100).toFixed(2)
const NEW_PROPOSAL = 'c'.repeat(64)
const WAITING_PROPOSAL = 'd'.repeat(64)
const secondsFrom = (now: string, until: string) => Math.max(0, Math.floor((Date.parse(until) - Date.parse(now)) / 1000))

interface IdeaOver {
  candidate_id: string
  status: string
  summary: string
  valid_until: string
  proposal_status?: string
  proposal_id?: string | null
  hints?: string[]
}

/** One `Idea` of `trex-idea-list/1`, built on the option proposal fixture. */
function idea(now: string, over: IdeaOver) {
  const option = optionProposal()
  const args = { program_id: option.program_id, candidate_id: over.candidate_id, asset_kind: 'option' }
  return {
    ...args,
    underlying: 'SPY',
    intent_id: over.candidate_id,
    strategy_version: option.strategy_version,
    summary: over.summary,
    option: null,
    equity: null,
    max_loss_cents: option.review.max_loss_cents,
    notional_cents: option.review.notional_cents,
    written_at: now,
    age_s: 0,
    entry_session: now.slice(0, 10),
    valid_until: over.valid_until,
    seconds_to_expiry: secondsFrom(now, over.valid_until),
    status: over.status,
    proposal_status: over.proposal_status ?? 'none',
    proposal_id: over.proposal_id ?? null,
    hints: over.hints ?? [],
    compose_args: args,
  }
}

/** A `GET api/ideas` document built from the fixtures. */
function ideasDoc() {
  const now = proposalListFixture().now
  const equity = equityEffect()
  return {
    schema: 'trex-idea-list/1',
    now,
    drafts: {
      status: 'ok',
      unavailable_reason: null as string | null,
      items: [
        idea(now, { candidate_id: optionProposal().effect.intent_id, status: 'available', summary: 'Buy 1 SPY debit vertical', valid_until: '2026-10-22T15:30:00Z' }),
        idea(now, { candidate_id: 'spy-stale-1', status: 'expired', summary: 'Buy 1 SPY debit vertical (stale)', valid_until: '2026-10-22T14:00:00Z' }),
        idea(now, { candidate_id: 'spy-used-1', status: 'used', summary: 'Buy 1 SPY debit vertical (sent)', valid_until: '2026-10-22T15:30:00Z', proposal_status: 'consumed', proposal_id: 'e'.repeat(64) }),
        idea(now, { candidate_id: 'spy-waiting-1', status: 'proposed', summary: 'Buy 1 SPY debit vertical (waiting)', valid_until: '2026-10-22T15:30:00Z', proposal_status: 'pending', proposal_id: WAITING_PROPOSAL }),
      ],
      blocked: [{ candidate_id: 'spy-credit-1', kind: 'credit_vertical', reason: 'credit_open_not_supported_v1', written_at: now }],
      problems: [] as { file: string; reason: string }[],
      truncated: false,
    },
    manual_equity: {
      candidate_prefix: 'manual:',
      program_id: 'owner-manual',
      strategy_version: 'owner-manual/1',
      symbols: [equity.symbol],
      limit_policies: ['ask', 'mid', 'limit_cents'],
      max_entries_per_session: 3,
      window_status: 'open',
      window_session: now.slice(0, 10),
      window_opens_at: '2026-10-22T13:50:00Z',
      window_closes_at: '2026-10-22T15:30:00Z',
      hints: [] as string[],
      // the hold cap: 20 NYSE sessions after Thursday 2026-10-22, hand counted
      max_hold_sessions: 20,
      max_exit_deadline_session: '2026-11-19' as string | null,
    },
    execution_enabled: false,
  }
}

interface Desk {
  /** The ideas document served (null: the route answers 404). */
  ideas?: object | null
  /** What GET api/commands/{key} answers for a POSTed body. */
  answer?: (body: string, gets: number) => CommandResult
  /** Replaces the intake's 202 `accepted` answer to a POST. */
  intake?: (body: string) => Response
  dropFirstPost?: boolean
}

function serve(desk: Desk = {}) {
  const ideas = desk.ideas === undefined ? ideasDoc() : desk.ideas
  let gets = 0
  let drop = desk.dropFirstPost ?? false
  const lastBody = { text: '' }
  return installFetch((call: FakeCall) => {
    if (call.method === 'GET' && call.url === 'api/proposals') return fakeResponse(200, proposalListFixture())
    if (call.method === 'GET' && call.url === 'api/today') return fakeResponse(200, todayFixture())
    if (call.method === 'GET' && call.url === 'api/ideas') {
      return ideas ? fakeResponse(200, ideas) : fakeResponse(404, { detail: 'Not Found' })
    }
    if (call.method === 'POST' && call.url === 'api/commands') {
      if (drop) {
        drop = false
        throw new TypeError('Failed to fetch')
      }
      lastBody.text = call.body!
      return desk.intake ? desk.intake(call.body!) : fakeResponse(202, resultFor(call.body!, 'accepted'))
    }
    if (call.method === 'GET' && call.url.startsWith('api/commands/')) {
      gets += 1
      return fakeResponse(200, desk.answer ? desk.answer(lastBody.text, gets) : resultFor(lastBody.text, 'accepted'))
    }
    return fakeResponse(404, { error: 'unknown' })
  })
}

async function mount(desk: Desk = {}) {
  const calls = serve(desk)
  render(<TradeReviewPage proposalId={null} />)
  await flush()
  return calls
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(Date.UTC(2026, 9, 22, 14, 2, 0))
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

const ideasRegion = () => screen.getByRole('region', { name: 'Ideas' })
const ticketRegion = () => screen.getByRole('region', { name: 'New SPY trade' })
const ideaItem = (candidate: string) => {
  const item = within(ideasRegion())
    .getAllByRole('listitem')
    .find((li) => li.getAttribute('data-candidate') === candidate)
  if (!item) throw new Error(`idea ${candidate} not listed`)
  return item
}
const sentBodies = (calls: FakeCall[]) => posts(calls).map((c) => JSON.parse(c.body!) as Record<string, unknown>)

const applied = (body: string): CommandResult =>
  resultFor(body, 'applied', {
    result: { answered_by: 'trex-desk', proposal_id: NEW_PROPOSAL, send_deadline: '2026-10-22T14:12:00Z' },
  })

describe('Ideas: prepare a proposal from a qualified idea', () => {
  it('Prepare proposal queues exactly one compose_entry with the idea compose_args, issued at the desk clock', async () => {
    const calls = await mount()
    const option = optionProposal()
    const item = ideaItem(option.effect.intent_id)
    expect(item.textContent).toContain('Buy 1 SPY debit vertical')
    expect(item.textContent).toContain(`$${dollars(option.review.max_loss_cents)}`)
    fireEvent.click(within(item).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    const bodies = sentBodies(calls)
    expect(bodies).toHaveLength(1)
    expect(bodies[0].kind).toBe('compose_entry')
    expect(bodies[0].args).toEqual({ program_id: option.program_id, candidate_id: option.effect.intent_id, asset_kind: 'option' })
    expect(bodies[0].issued_at).toBe(proposalListFixture().now)
    expect(within(item).getByRole('status').textContent).toMatch(/Queued for the desk owner/)
  })

  it('once the desk prepares it, links to the new proposal for the separate approval; nothing is approved here', async () => {
    const calls = await mount({ answer: (body) => applied(body) })
    const before = calls.filter((c) => c.url === 'api/proposals').length
    const item = ideaItem(optionProposal().effect.intent_id)
    fireEvent.click(within(item).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    await flush(2000)
    const link = within(item).getByRole('link', { name: /Review and approve/ })
    expect(link.getAttribute('href')).toBe(`#/trade-review/${NEW_PROPOSAL}`)
    expect(item.textContent).toMatch(/Nothing is approved or sent/)
    expect(calls.filter((c) => c.url === 'api/proposals').length).toBeGreaterThan(before)
    expect(sentBodies(calls).map((b) => b.kind)).toEqual(['compose_entry'])
    expect(screen.queryByRole('button', { name: 'Approve entry' })).toBeNull()
    await flush(30_000)
    expect(sentBodies(calls).map((b) => b.kind)).toEqual(['compose_entry'])
  })

  it('a retry after a network drop re-sends the same bytes under the same key', async () => {
    const calls = await mount({ dropFirstPost: true })
    const item = ideaItem(optionProposal().effect.intent_id)
    fireEvent.click(within(item).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    expect(within(item).getByRole('status').textContent).toMatch(/Not confirmed/)
    fireEvent.click(within(item).getByRole('button', { name: 'Try again (same request)' }))
    await flush()
    const sent = posts(calls)
    expect(sent).toHaveLength(2)
    expect(sent[1].body).toBe(sent[0].body)
    expect(within(item).queryByRole('button', { name: 'Prepare proposal' })).toBeNull()
  })

  it('a desk refusal shows its words, the code and the other blockers, and allows a fresh start', async () => {
    const calls = await mount({
      answer: (body) =>
        resultFor(body, 'refused', {
          reason: 'candidate_expired',
          result: { answered_by: 'trex-desk', blockers: ['candidate_expired', 'entries_paused'] },
        }),
    })
    const item = ideaItem(optionProposal().effect.intent_id)
    fireEvent.click(within(item).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    await flush(2000)
    const status = within(item).getByRole('status').textContent ?? ''
    expect(status).toContain('candidate_expired')
    expect(status).toContain('entries_paused')
    expect(status).toMatch(/Refused/)
    fireEvent.click(within(item).getByRole('button', { name: 'Start over' }))
    fireEvent.click(within(item).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    const keys = sentBodies(calls).map((b) => b.idempotency_key)
    expect(keys).toHaveLength(2)
    expect(keys[1]).not.toBe(keys[0])
  })

  it('expired, used and blocked ideas say why and offer nothing to press', async () => {
    const calls = await mount()
    for (const [candidate, words] of [
      ['spy-stale-1', /Expired/],
      ['spy-used-1', /Already used/],
    ] as const) {
      const item = ideaItem(candidate)
      expect(item.textContent).toMatch(words)
      expect(within(item).queryByRole('button', { name: 'Prepare proposal' })).toBeNull()
    }
    const region = ideasRegion().textContent ?? ''
    expect(region).toMatch(/Blocked by the desk/)
    expect(region).toContain('spy-credit-1')
    expect(region).toContain('credit_open_not_supported_v1')
    expect(posts(calls)).toHaveLength(0)
  })

  it('an idea with a waiting proposal links to it for approval instead of preparing another', async () => {
    await mount()
    const item = ideaItem('spy-waiting-1')
    expect(within(item).queryByRole('button', { name: 'Prepare proposal' })).toBeNull()
    expect(within(item).getByRole('link', { name: /review and approve/ }).getAttribute('href')).toBe(
      `#/trade-review/${WAITING_PROPOSAL}`,
    )
  })

  it('advisory hints show in words and do not stop the owner from asking', async () => {
    const doc = ideasDoc()
    doc.drafts.items[0].hints = ['entries_paused']
    const calls = await mount({ ideas: doc })
    const item = ideaItem(optionProposal().effect.intent_id)
    expect(item.textContent).toMatch(/advisory/)
    expect(item.textContent).toContain('New entries are paused')
    fireEvent.click(within(item).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    expect(posts(calls)).toHaveLength(1)
  })

  it('an idea whose window closes while on screen can no longer be prepared', async () => {
    const doc = ideasDoc()
    doc.drafts.items[0].seconds_to_expiry = 5
    await mount({ ideas: doc })
    const item = ideaItem(optionProposal().effect.intent_id)
    expect(within(item).getByRole('button', { name: 'Prepare proposal' })).toBeTruthy()
    await flush(6000)
    expect(within(item).queryByRole('button', { name: 'Prepare proposal' })).toBeNull()
    expect(item.textContent).toMatch(/Expired/)
  })

  it('when the ideas list cannot load, the page says so and invents none', async () => {
    await mount({ ideas: null })
    expect(ideasRegion().textContent).toMatch(/could not be loaded/)
    expect(within(ideasRegion()).queryByRole('button', { name: 'Prepare proposal' })).toBeNull()
  })

  it('an unavailable drafts block shows its reason', async () => {
    const doc = ideasDoc()
    doc.drafts = { ...doc.drafts, status: 'unavailable', unavailable_reason: 'proposals_dir_unreadable', items: [], blocked: [] }
    await mount({ ideas: doc })
    expect(ideasRegion().textContent).toContain('proposals_dir_unreadable')
    expect(ideasRegion().textContent).toContain('The proposals folder could not be read')
  })
})

/** Fill the ticket with the equity effect fixture's numbers. */
function fillTicket(over: { quantity?: string; policy?: 'ask' | 'mid' | 'limit'; limit?: string; stop?: string; target?: string; deadline?: string } = {}) {
  const e = equityEffect()
  const region = ticketRegion()
  fireEvent.change(within(region).getByLabelText('Shares'), { target: { value: over.quantity ?? String(e.quantity) } })
  const policy = over.policy ?? 'limit'
  const label = { limit: 'My limit', ask: 'Pay the ask', mid: 'Midpoint' }[policy]
  fireEvent.click(within(region).getByRole('radio', { name: new RegExp(`^${label}`) }))
  if (policy === 'limit') {
    fireEvent.change(within(region).getByLabelText('Limit price ($)'), { target: { value: over.limit ?? dollars(e.limit_cents) } })
  }
  fireEvent.change(within(region).getByLabelText('Stop price ($)'), {
    target: { value: over.stop ?? dollars(e.exit_policy.stop_price_cents) },
  })
  fireEvent.change(within(region).getByLabelText('Target price ($, optional)'), { target: { value: over.target ?? '' } })
  fireEvent.change(within(region).getByLabelText('Exit by the end of'), {
    target: { value: over.deadline ?? e.exit_policy.exit_deadline_session },
  })
}

describe('New SPY trade: the manual ticket', () => {
  it('says plainly: paper only, cash only, real-time quote, the entry window, and that composing never approves', async () => {
    await mount()
    const text = ticketRegion().textContent ?? ''
    for (const words of [/Paper account only/, /Cash only/, /real-time/, /Entry window\. Open now, 09:50 to 11:30 ET/, /never approves or sends/, /At most 3 entries per session/]) {
      expect(text).toMatch(words)
    }
  })

  it('queues one compose_entry carrying the ticket: whole shares, limit, stop, target and deadline in integer cents', async () => {
    const calls = await mount()
    const e = equityEffect()
    fillTicket({ target: dollars(e.exit_policy.target_price_cents!) })
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    const bodies = sentBodies(calls)
    expect(bodies).toHaveLength(1)
    const body = bodies[0]
    expect(body.kind).toBe('compose_entry')
    expect(body.args).toEqual({
      program_id: 'owner-manual',
      candidate_id: `manual:${String(body.idempotency_key)}`,
      asset_kind: 'equity',
      manual_equity_ticket: {
        symbol: e.symbol,
        quantity: e.quantity,
        stop_price_cents: e.exit_policy.stop_price_cents,
        target_price_cents: e.exit_policy.target_price_cents,
        exit_deadline_session: e.exit_policy.exit_deadline_session,
        limit_policy: { limit_cents: e.limit_cents },
      },
    })
    expect(body.issued_at).toBe(proposalListFixture().now)
  })

  it('refuses fractional shares, a stop at or above the limit and a missing deadline; nothing is sent', async () => {
    const calls = await mount()
    const e = equityEffect()
    fillTicket({ quantity: '1.5', stop: dollars(e.limit_cents), deadline: '' })
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    const text = ticketRegion().textContent ?? ''
    expect(text).toMatch(/Whole shares only/)
    expect(text).toMatch(/The stop must be below the entry price/)
    expect(text).toMatch(/Choose the last session/)
    expect(posts(calls)).toHaveLength(0)
  })

  it('takes a free date not before today (the desk validates the session)', async () => {
    const calls = await mount()
    const picker = within(ticketRegion()).getByLabelText('Exit by the end of') as HTMLInputElement
    expect(picker.type).toBe('date')
    expect(picker.min).toBe('2026-10-22')
    fillTicket({ deadline: '2026-10-21' })
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    expect(ticketRegion().textContent).toMatch(/cannot be before today/)
    expect(posts(calls)).toHaveLength(0)
  })

  it('the date picker stops at the latest deadline the desk allows (its hold cap); past it nothing is sent', async () => {
    const calls = await mount()
    const picker = within(ticketRegion()).getByLabelText('Exit by the end of') as HTMLInputElement
    expect(picker.max).toBe('2026-11-19')
    expect(ticketRegion().textContent).toMatch(/Hold it at most 20 market sessions after today: 2026-11-19 at the latest/)
    fillTicket({ deadline: '2026-11-20' })
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    expect(ticketRegion().textContent).toMatch(/Choose a session at most 20 market sessions after today: 2026-11-19 at the latest/)
    expect(posts(calls)).toHaveLength(0)
    fillTicket({ deadline: '2026-11-19' })
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    expect(posts(calls)).toHaveLength(1)
  })

  it('a desk refusal names its reason and its detail code in words', async () => {
    await mount({
      answer: (body) => resultFor(body, 'refused', { reason: 'manual_ticket_invalid', detail: 'deadline_beyond_max_hold' }),
    })
    fillTicket()
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    await flush(2000)
    const status = within(ticketRegion()).getByRole('status').textContent ?? ''
    expect(status).toMatch(/^Refused: /)
    expect(status).toContain('manual_ticket_invalid')
    expect(status).toMatch(/more than 20 market sessions/)
    expect(status).toContain('deadline_beyond_max_hold')
  })

  it('when the desk lists upcoming sessions, the picker offers exactly those', async () => {
    const doc = ideasDoc()
    const sessions = ['2026-10-22', equityEffect().exit_policy.exit_deadline_session, '2026-10-26']
    const calls = await mount({ ideas: { ...doc, manual_equity: { ...doc.manual_equity, sessions } } })
    const picker = within(ticketRegion()).getByLabelText('Exit by the end of') as HTMLSelectElement
    expect(Array.from(picker.options).map((o) => o.value).filter((v) => v !== '')).toEqual(sessions)
    fillTicket()
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    expect(posts(calls)).toHaveLength(1)
  })

  it('a retry re-sends the same bytes: the same key and so the same candidate', async () => {
    const calls = await mount({ dropFirstPost: true })
    fillTicket({ policy: 'mid' })
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    // the decision is frozen while it is unconfirmed
    expect(within(ticketRegion()).getByLabelText('Shares').matches(':disabled')).toBe(true)
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Try again (same request)' }))
    await flush()
    const sent = posts(calls)
    expect(sent).toHaveLength(2)
    expect(sent[1].body).toBe(sent[0].body)
    const body = JSON.parse(sent[1].body!) as { idempotency_key: string; args: { candidate_id: string; manual_equity_ticket: { limit_policy: unknown } } }
    expect(body.args.candidate_id).toBe(`manual:${body.idempotency_key}`)
    expect(body.args.manual_equity_ticket.limit_policy).toBe('mid')
  })

  it('links the prepared proposal for approval and never sends an approval itself', async () => {
    const calls = await mount({ answer: (body) => applied(body) })
    fillTicket()
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    await flush(2000)
    const link = within(ticketRegion()).getByRole('link', { name: /Review and approve/ })
    expect(link.getAttribute('href')).toBe(`#/trade-review/${NEW_PROPOSAL}`)
    await flush(30_000)
    expect(sentBodies(calls).map((b) => b.kind)).toEqual(['compose_entry'])
  })

  it('advisory hints and a closed window are said in words; the desk stays the judge', async () => {
    const doc = ideasDoc()
    doc.manual_equity = { ...doc.manual_equity, window_status: 'closed', window_closes_at: '2026-10-22T14:00:00Z', hints: ['entry_window_closed', 'entries_paused'] }
    const calls = await mount({ ideas: doc })
    const text = ticketRegion().textContent ?? ''
    expect(text).toMatch(/Closed for today/)
    expect(text).toContain('entry_window_closed')
    expect(text).toContain('New entries are paused')
    fillTicket()
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    expect(posts(calls)).toHaveLength(1)
  })

  it('an intake refusal (the desk does not know the ticket yet) is shown, not swallowed', async () => {
    // the backend's answer to an args field it does not know (extra="forbid")
    const calls = await mount({
      intake: () =>
        fakeResponse(422, {
          error: 'invalid_command',
          errors: [{ loc: ['compose_entry', 'args', 'manual_equity_ticket'], type: 'extra_forbidden', msg: 'x' }],
        }),
    })
    fillTicket()
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    expect(posts(calls)).toHaveLength(1)
    const status = within(ticketRegion()).getByRole('status').textContent ?? ''
    expect(status).toContain('invalid_command')
    expect(status).toContain('HTTP 422')
    expect(within(ticketRegion()).getByRole('button', { name: 'Start over' })).toBeTruthy()
  })

  it('without the idea list it still works from the proposal list clock, and says the desk checks the terms', async () => {
    const calls = await mount({ ideas: null })
    expect(ticketRegion().textContent).toMatch(/ticket terms could not be loaded/)
    fillTicket()
    fireEvent.click(within(ticketRegion()).getByRole('button', { name: 'Prepare proposal' }))
    await flush()
    const bodies = sentBodies(calls)
    expect(bodies).toHaveLength(1)
    expect(bodies[0].issued_at).toBe(proposalListFixture().now)
    expect((bodies[0].args as { program_id: string }).program_id).toBe('owner-manual')
  })
})
