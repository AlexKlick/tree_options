import { describe, expect, it } from 'vitest'
import {
  IDEAS_PATH,
  KEYS,
  MANUAL_CANDIDATE_PREFIX,
  buildCommandBody,
  isKnown,
  manualCandidateId,
  type CommandBody,
  type IdeaList,
  type ManualEquityTicket,
} from './contracts'

// Wave 5 shared fixtures (tests/contracts/test_wave5_contracts.py round-trips
// the same files through the Python models; idea_list.json is the document
// tests/unit/test_trex_web_entry_ideas.py serves).
const wave5 = import.meta.glob('../../../tests/contracts/fixtures/wave5/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>

type Json = Record<string, unknown>

function fixture(name: string): Json {
  const hit = Object.entries(wave5).find(([path]) => path.endsWith(`/${name}`))
  if (!hit) throw new Error(`fixture ${name} not found`)
  return structuredClone(hit[1]) as Json
}

function expectKeys(value: unknown, keys: readonly string[], where: string): void {
  expect(value, where).toBeTypeOf('object')
  expect(Object.keys(value as Json).sort(), where).toEqual([...keys].sort())
}

describe('trex-idea-list/1 key lists match the shared fixtures', () => {
  it('a known list: every idea, structure, blocked row and problem', () => {
    const doc = fixture('idea_list.json')
    expectKeys(doc, KEYS.IdeaList, 'list')
    const drafts = doc.drafts as Json
    expectKeys(drafts, KEYS.DraftIdeas, 'drafts')
    const items = drafts.items as Json[]
    expect(items.map((i) => i.asset_kind).sort()).toEqual(['equity', 'option'])
    for (const item of items) {
      expectKeys(item, KEYS.Idea, `idea ${item.candidate_id}`)
      const args = item.compose_args as Json
      expect(Object.keys(args).sort()).toEqual(KEYS.ComposeEntryArgs.filter((k) => k !== 'manual_equity_ticket').sort())
      expect(args.candidate_id).toBe(item.candidate_id)
      if (item.asset_kind === 'option') {
        expectKeys(item.option, KEYS.IdeaOption, 'option')
        for (const leg of (item.option as Json).legs as Json[]) expectKeys(leg, KEYS.IdeaLeg, 'leg')
        expect(item.equity).toBeNull()
      } else {
        expectKeys(item.equity, KEYS.IdeaEquity, 'equity')
        expect(item.option).toBeNull()
      }
    }
    for (const row of drafts.blocked as Json[]) expectKeys(row, KEYS.BlockedIdea, 'blocked')
    for (const row of drafts.problems as Json[]) expectKeys(row, KEYS.IdeaProblem, 'problem')
    expectKeys(doc.manual_equity, KEYS.ManualEquityTerms, 'manual terms')
  })

  it('an unavailable drafts block keeps every key, all null, and the manual terms', () => {
    const doc = fixture('idea_list_unavailable.json') as unknown as IdeaList
    expectKeys(doc.drafts, KEYS.DraftIdeas, 'drafts')
    expect(isKnown(doc.drafts)).toBe(false)
    expect(doc.drafts.items).toBeNull()
    expect(doc.manual_equity.window_status).toBe('no_session')
    expect(doc.manual_equity.window_session).toBeNull()
  })

  it('the manual compose command and its ticket', () => {
    const command = fixture('command_compose_entry_manual.json')
    const args = command.args as Json
    expectKeys(args, KEYS.ComposeEntryArgs, 'compose args')
    expectKeys(args.manual_equity_ticket, KEYS.ManualEquityTicket, 'ticket')
  })
})

describe('Wave 5 constants and helpers', () => {
  it('constants equal the Python ones the fixtures carry', () => {
    const list = fixture('idea_list.json') as unknown as IdeaList
    expect(IDEAS_PATH).toBe('/api/ideas')
    expect(MANUAL_CANDIDATE_PREFIX).toBe(list.manual_equity.candidate_prefix)
    expect(list.schema).toBe('trex-idea-list/1')
    expect(list.execution_enabled).toBe(false)
  })

  it('manualCandidateId takes a lower-case UUID only', () => {
    const id = '3b0d6c1e-8f2a-4c5d-9e7f-1a2b3c4d5e6f'
    expect(manualCandidateId(id)).toBe(`manual:${id}`)
    expect(() => manualCandidateId(id.toUpperCase())).toThrow(RangeError)
    expect(() => manualCandidateId('manual:' + id)).toThrow(RangeError)
  })

  it('buildCommandBody carries a manual ticket into the fixture shape', () => {
    const golden = fixture('command_compose_entry_manual.json')
    const goldenArgs = golden.args as Json
    const ticket = goldenArgs.manual_equity_ticket as unknown as ManualEquityTicket
    const body: CommandBody<'compose_entry'> = buildCommandBody(
      'compose_entry',
      {
        program_id: goldenArgs.program_id as string,
        candidate_id: goldenArgs.candidate_id as string,
        asset_kind: 'equity',
        sleeve_ref: null,
        manual_equity_ticket: ticket,
      },
      golden.idempotency_key as string,
      new Date(golden.issued_at as string),
      120,
    )
    const unstamped: Json = { ...golden }
    delete unstamped.issued_by // the backend stamps it; a body carrying it is refused
    expect(body).toEqual(unstamped)
  })
})
