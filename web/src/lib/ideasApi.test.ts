import { describe, expect, it } from 'vitest'
import { COMMAND_MAX_TTL_S, KEYS, type EntryProposalDoc } from './contracts'
import {
  EMPTY_TICKET,
  IDEAS_URL,
  MANUAL_PROGRAM_ID,
  composeArgsForIdea,
  composeArgsForTicket,
  composeTtlS,
  composedProposalId,
  entryHintCents,
  entryWindowState,
  etDate,
  parseDollars,
  parseIdeas,
  prepareCompose,
  validateTicket,
  type TicketContext,
  type TicketForm,
} from './ideasApi'
import { appliedResultFixture, proposalListFixture } from './reviewTestKit'

const KEY = '6f1c2b8e-3d4a-4f5b-9c6d-7e8f9a0b1c2d'

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
const equity = () => fixture<EquityEffect>('equity_effect.json')
const equityQuote = () => fixture<EntryProposalDoc>('proposal_equity.json').review.quote
const optionProposal = () => fixture<EntryProposalDoc>('proposal_option.json')
const dollars = (cents: number): string => (cents / 100).toFixed(2)
const listNow = () => proposalListFixture().now

function ctx(over: Partial<TicketContext> = {}): TicketContext {
  const e = equity()
  return {
    now: listNow(),
    symbol: e.symbol,
    sessions: [etDate(listNow()), e.exit_policy.exit_deadline_session],
    quote: equityQuote(),
    ...over,
  }
}

/** The equity effect fixture typed in as the owner would (limit policy). */
function fixtureForm(over: Partial<TicketForm> = {}): TicketForm {
  const e = equity()
  return {
    ...EMPTY_TICKET,
    quantity: String(e.quantity),
    limitPolicy: 'limit',
    limitPrice: dollars(e.limit_cents),
    stopPrice: dollars(e.exit_policy.stop_price_cents),
    targetPrice: e.exit_policy.target_price_cents === null ? '' : dollars(e.exit_policy.target_price_cents),
    deadline: e.exit_policy.exit_deadline_session,
    ...over,
  }
}

describe('the ideas URL', () => {
  it('is relative, so it resolves under / and /trex/ alike', () => {
    expect(IDEAS_URL).toBe('api/ideas')
  })
})

describe('money typed by the owner', () => {
  it('becomes integer cents; a sub-cent or malformed price is refused, never rounded', () => {
    expect(parseDollars('671.50')).toBe(67150)
    expect(parseDollars('671.5')).toBe(67150)
    expect(parseDollars(' $665 ')).toBe(66500)
    expect(parseDollars('1,234.05')).toBe(123405)
    expect(parseDollars('0.07')).toBe(7)
    for (const bad of ['671.505', 'abc', '', '-5', '6.7.1', '1e3']) expect(parseDollars(bad), bad).toBeNull()
  })
})

describe('the entry price a limit policy implies', () => {
  it('is the owner limit, the ask, or the midpoint rounded up; unknown without a quote', () => {
    const q = equityQuote()
    expect(entryHintCents('limit', 67150, q)).toBe(67150)
    expect(entryHintCents('ask', null, q)).toBe(q.ask_cents)
    expect(entryHintCents('mid', null, q)).toBe(Math.ceil((q.bid_cents + q.ask_cents) / 2))
    expect(entryHintCents('mid', null, { ...q, bid_cents: 100, ask_cents: 103 })).toBe(102)
    expect(entryHintCents('ask', null, null)).toBeNull()
    expect(entryHintCents('mid', null, null)).toBeNull()
  })
})

describe('validateTicket', () => {
  it('turns the equity fixture, typed in, into exactly its exit policy and an explicit limit', () => {
    const e = equity()
    const check = validateTicket(fixtureForm(), ctx())
    expect(check).toEqual({
      ok: true,
      entryHintCents: e.limit_cents,
      ticket: {
        symbol: e.symbol,
        quantity: e.quantity,
        stop_price_cents: e.exit_policy.stop_price_cents,
        target_price_cents: e.exit_policy.target_price_cents,
        exit_deadline_session: e.exit_policy.exit_deadline_session,
        limit_policy: { limit_cents: e.limit_cents },
      },
    })
  })

  it('an ask or midpoint ticket names the policy only; the desk sets the limit from its live quote', () => {
    for (const policy of ['ask', 'mid'] as const) {
      const check = validateTicket(fixtureForm({ limitPolicy: policy, limitPrice: '999.99', targetPrice: '' }), ctx())
      expect(check.ok, policy).toBe(true)
      if (check.ok) {
        expect(check.ticket.limit_policy).toBe(policy)
        expect(check.ticket.target_price_cents).toBeNull()
      }
    }
  })

  it('refuses fractional, zero and non-numeric share counts', () => {
    for (const quantity of ['1.5', '2,5', '0.', '.5']) {
      const check = validateTicket(fixtureForm({ quantity }), ctx())
      expect(check.ok, quantity).toBe(false)
      if (!check.ok) expect(check.errors.quantity, quantity).toMatch(/Whole shares only/)
    }
    for (const quantity of ['0', '', 'ten', '-3']) {
      const check = validateTicket(fixtureForm({ quantity }), ctx())
      expect(check.ok, quantity).toBe(false)
      if (!check.ok) expect(check.errors.quantity, quantity).toBeTruthy()
    }
  })

  it('the stop must sit below the entry price the policy implies', () => {
    const q = equityQuote()
    // limit policy: at the limit is refused, one cent under passes
    const limit = equity().limit_cents
    let check = validateTicket(fixtureForm({ stopPrice: dollars(limit) }), ctx())
    expect(check.ok).toBe(false)
    if (!check.ok) expect(check.errors.stopPrice).toMatch(/below the entry price/)
    check = validateTicket(fixtureForm({ stopPrice: dollars(limit - 1) }), ctx())
    expect(check.ok).toBe(true)
    // ask policy with a quote: judged against the ask
    check = validateTicket(fixtureForm({ limitPolicy: 'ask', stopPrice: dollars(q.ask_cents), targetPrice: '' }), ctx())
    expect(check.ok).toBe(false)
    if (!check.ok) expect(check.errors.stopPrice).toMatch(/below the entry price/)
    check = validateTicket(fixtureForm({ limitPolicy: 'ask', stopPrice: dollars(q.ask_cents - 1), targetPrice: '' }), ctx())
    expect(check.ok).toBe(true)
    // no quote (the idea list carries none today): the desk is the judge
    check = validateTicket(fixtureForm({ limitPolicy: 'ask', stopPrice: dollars(q.ask_cents + 500), targetPrice: '' }), ctx({ quote: null }))
    expect(check.ok).toBe(true)
  })

  it('a target must sit above the entry price and the stop; blank means none', () => {
    const limit = equity().limit_cents
    let check = validateTicket(fixtureForm({ targetPrice: dollars(limit) }), ctx())
    expect(check.ok).toBe(false)
    if (!check.ok) expect(check.errors.targetPrice).toMatch(/above the entry price/)
    expect(validateTicket(fixtureForm({ targetPrice: dollars(limit + 1) }), ctx()).ok).toBe(true)
    const stop = equity().exit_policy.stop_price_cents
    check = validateTicket(fixtureForm({ limitPolicy: 'ask', targetPrice: dollars(stop) }), ctx({ quote: null }))
    expect(check.ok).toBe(false)
    if (!check.ok) expect(check.errors.targetPrice).toMatch(/above the stop/)
  })

  it('needs a limit price exactly when the policy is limit', () => {
    const check = validateTicket(fixtureForm({ limitPrice: '' }), ctx())
    expect(check.ok).toBe(false)
    if (!check.ok) expect(check.errors.limitPrice).toBeTruthy()
  })

  it('needs a deadline: one of the listed sessions, or (no list) not before today', () => {
    let check = validateTicket(fixtureForm({ deadline: '' }), ctx())
    expect(check.ok).toBe(false)
    if (!check.ok) expect(check.errors.deadline).toMatch(/last session/)
    check = validateTicket(fixtureForm({ deadline: '2026-10-24' }), ctx()) // a Saturday, not listed
    expect(check.ok).toBe(false)
    if (!check.ok) expect(check.errors.deadline).toMatch(/upcoming market sessions/)
    check = validateTicket(fixtureForm({ deadline: '2026-10-21' }), ctx({ sessions: null }))
    expect(check.ok).toBe(false)
    if (!check.ok) expect(check.errors.deadline).toMatch(/before today/)
    expect(validateTicket(fixtureForm({ deadline: etDate(listNow()) }), ctx({ sessions: null })).ok).toBe(true)
  })

  it('reads today in New York, not on this device', () => {
    expect(etDate('2026-10-23T03:30:00Z')).toBe('2026-10-22')
    expect(etDate('2026-10-22T14:02:00Z')).toBe('2026-10-22')
  })
})

describe('compose args and the frozen command', () => {
  it('an idea composes with the server compose_args verbatim, else exactly its own ids', () => {
    const own = { program_id: 'desk-miner', candidate_id: 'cand-1', asset_kind: 'option' as const }
    expect(composeArgsForIdea(own)).toEqual(own)
    const withSleeve = { ...own, sleeve_ref: 'sleeve-a' }
    expect(composeArgsForIdea({ ...own, compose_args: withSleeve })).toEqual(withSleeve)
  })

  it('a manual ticket composes as an equity under the manual program, named manual:<request key>', () => {
    const check = validateTicket(fixtureForm(), ctx())
    if (!check.ok) throw new Error('fixture form must validate')
    const args = composeArgsForTicket(check.ticket, KEY)
    expect(args).toEqual({
      program_id: MANUAL_PROGRAM_ID,
      candidate_id: `manual:${KEY}`,
      asset_kind: 'equity',
      manual_equity_ticket: check.ticket,
    })
    // the backend lane's rule: manual:<lower-case uuid>
    expect(args.candidate_id).toMatch(/^manual:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
    expect(composeArgsForTicket(check.ticket, KEY, 'eq-manual-2').program_id).toBe('eq-manual-2')
  })

  it('prepares only compose_entry, at the desk clock, never past the idea horizon', () => {
    const args = composeArgsForIdea({ program_id: 'p', candidate_id: 'c', asset_kind: 'equity' })
    const prepared = prepareCompose(args, Date.parse(listNow()), composeTtlS(42), KEY)
    expect(prepared.kind).toBe('compose_entry')
    expect(prepared.key).toBe(KEY)
    const sent = JSON.parse(prepared.text) as Record<string, unknown>
    expect(Object.keys(sent).sort()).toEqual([...KEYS.CommandBody].sort())
    expect(sent).toMatchObject({ kind: 'compose_entry', args, issued_at: listNow(), expires_at: '2026-10-22T14:02:42Z' })
    expect(sent).not.toHaveProperty('issued_by')
    expect(composeTtlS(null)).toBe(COMMAND_MAX_TTL_S)
    expect(composeTtlS(900)).toBe(COMMAND_MAX_TTL_S)
    expect(() => composeTtlS(0)).toThrow(RangeError)
  })

  it('reads the new proposal id only from an applied compose_entry', () => {
    const id = 'c'.repeat(64)
    const base = { ...appliedResultFixture(), kind: 'compose_entry' as const, result: { proposal_id: id } }
    expect(composedProposalId(base)).toBe(id)
    expect(composedProposalId({ ...base, status: 'refused' })).toBeNull()
    expect(composedProposalId({ ...base, kind: 'approve_entry' })).toBeNull()
    expect(composedProposalId({ ...base, result: { proposal_id: 'not-a-hash' } })).toBeNull()
    expect(composedProposalId({ ...base, result: null })).toBeNull()
  })
})

/** A `trex-idea-list/1` document (the backend lane's shape) from the fixtures. */
function ideaListDoc() {
  const option = optionProposal()
  const args = { program_id: option.program_id, candidate_id: option.effect.intent_id, asset_kind: 'option' }
  return {
    schema: 'trex-idea-list/1',
    now: listNow(),
    drafts: {
      status: 'ok',
      unavailable_reason: null,
      items: [
        {
          ...args,
          underlying: 'SPY',
          intent_id: option.effect.intent_id,
          strategy_version: option.strategy_version,
          summary: 'Buy 1 SPY debit vertical',
          option: null,
          equity: null,
          max_loss_cents: option.review.max_loss_cents,
          notional_cents: option.review.notional_cents,
          written_at: listNow(),
          age_s: 0,
          entry_session: '2026-10-22',
          valid_until: '2026-10-22T15:30:00Z',
          seconds_to_expiry: 5280,
          status: 'available',
          proposal_status: 'none',
          proposal_id: null,
          hints: ['entries_paused'],
          compose_args: { ...args, sleeve_ref: 'sleeve-a' },
        },
        { candidate_id: 'cand-3', asset_kind: 'option' },
        { ...args, candidate_id: 'cand-4', status: 'proposed', proposal_status: 'pending', proposal_id: 'd'.repeat(64), compose_args: args },
      ],
      blocked: [{ candidate_id: 'cand-2', kind: 'credit_vertical', reason: 'credit_open_not_supported_v1', written_at: listNow() }, 7],
      problems: [{ file: 'x.json', reason: 'candidate_invalid' }],
      truncated: false,
    },
    manual_equity: {
      candidate_prefix: 'manual:',
      program_id: 'owner-manual',
      strategy_version: 'owner-manual/1',
      symbols: ['SPY'],
      limit_policies: ['ask', 'mid', 'limit_cents'],
      max_entries_per_session: 3,
      window_status: 'open',
      window_session: '2026-10-22',
      window_opens_at: '2026-10-22T13:50:00Z',
      window_closes_at: '2026-10-22T15:30:00Z',
      hints: [],
    },
    execution_enabled: false,
  }
}

describe('parseIdeas (trex-idea-list/1)', () => {
  it('keeps every readable idea and counts (never drops) the rest', () => {
    const view = parseIdeas(ideaListDoc())
    expect(view.ideas.map((i) => i.candidate_id)).toEqual([optionProposal().effect.intent_id, 'cand-4'])
    expect(view.unreadable).toBe(2) // one idea and one blocked row
    expect(view.ideas[0]).toMatchObject({ status: 'available', seconds_to_expiry: 5280, hints: ['entries_paused'] })
    const option = optionProposal()
    expect(view.ideas[0].compose_args).toEqual({
      program_id: option.program_id,
      candidate_id: option.effect.intent_id,
      asset_kind: 'option',
      sleeve_ref: 'sleeve-a',
    })
    expect(view.ideas[1]).toMatchObject({ status: 'proposed', proposal_id: 'd'.repeat(64) })
    expect(view.blocked).toEqual([{ candidate_id: 'cand-2', kind: 'credit_vertical', reason: 'credit_open_not_supported_v1' }])
    expect(view.problems).toEqual([{ file: 'x.json', reason: 'candidate_invalid' }])
    expect(view.status).toBe('ok')
  })

  it('reads the manual terms: the limit_cents policy is the form choice limit', () => {
    const terms = parseIdeas(ideaListDoc()).manual_equity
    expect(terms).toMatchObject({
      program_id: 'owner-manual',
      symbols: ['SPY'],
      limit_policies: ['ask', 'mid', 'limit'],
      max_entries_per_session: 3,
      window_status: 'open',
      sessions: null,
      quote: null,
    })
  })

  it('compose_args that name another idea are not used', () => {
    const doc = ideaListDoc()
    const first = doc.drafts.items[0] as { compose_args: Record<string, unknown> }
    first.compose_args = { ...first.compose_args, candidate_id: 'someone-else' }
    expect(parseIdeas(doc).ideas[0].compose_args).toBeNull()
  })

  it('an unavailable drafts block keeps its reason and lists no idea', () => {
    const doc = { ...ideaListDoc(), drafts: { status: 'unavailable', unavailable_reason: 'proposals_dir_unreadable', items: null, blocked: null, problems: null, truncated: null } }
    const view = parseIdeas(doc)
    expect(view.status).toBe('unavailable')
    expect(view.unavailable_reason).toBe('proposals_dir_unreadable')
    expect(view.ideas).toEqual([])
    expect(view.manual_equity).not.toBeNull()
  })

  it('refuses a document that is not an idea list, or has no server time', () => {
    expect(() => parseIdeas({ ...ideaListDoc(), schema: 'trex-proposal-list/1' })).toThrow()
    expect(() => parseIdeas({ ...ideaListDoc(), now: 'later' })).toThrow()
    expect(() => parseIdeas(['trex-idea-list/1'])).toThrow()
  })
})

describe('the equity entry window, live at the desk clock', () => {
  const terms = () => parseIdeas(ideaListDoc()).manual_equity
  it('moves from not yet, to open, to closed as the clock passes the server window', () => {
    expect(entryWindowState(terms(), Date.parse('2026-10-22T13:49:59Z'))).toBe('not_yet')
    expect(entryWindowState(terms(), Date.parse('2026-10-22T13:50:00Z'))).toBe('open')
    expect(entryWindowState(terms(), Date.parse('2026-10-22T15:29:59Z'))).toBe('open')
    expect(entryWindowState(terms(), Date.parse('2026-10-22T15:30:00Z'))).toBe('closed')
  })
  it('a day without a session, or no terms at all, says so', () => {
    const none = { ...terms()!, window_status: 'no_session' as const, window_opens_at: null, window_closes_at: null }
    expect(entryWindowState(none, Date.parse(listNow()))).toBe('no_session')
    expect(entryWindowState(null, Date.parse(listNow()))).toBe('unknown')
  })
})

// Static guards on this lane's sources.
const sources = import.meta.glob(['./ideasApi.ts', '../components/TradeIdeas.tsx', '../components/trade-ideas.css'], {
  eager: true,
  query: '?raw',
  import: 'default',
}) as Record<string, string>

describe('lane sources', () => {
  it('exist', () => {
    expect(Object.keys(sources).length).toBe(3)
  })

  it.each(Object.entries(sources))('%s has no en or em dash', (_name, text) => {
    expect(text.includes(String.fromCharCode(0x2013))).toBe(false)
    expect(text.includes(String.fromCharCode(0x2014))).toBe(false)
  })

  it.each(Object.entries(sources))('%s never builds an approval', (_name, text) => {
    expect(text).not.toMatch(/['"]approve_entry['"]/)
  })
})
