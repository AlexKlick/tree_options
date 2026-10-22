import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { KEYS } from '../lib/contracts'
import { PILOT_ROW_KEYS, PILOT_VIEW_KEYS, sealCommandBody, type PilotView } from '../lib/pilotApi'
import { PilotPanel } from './PilotPage'

// The served view (tests/research/test_pilot_view.py pins this file to what
// GET /api/pilot returns): observation sealed, baseline draft with usage,
// challenger draft with unknown usage.
const fixtures = import.meta.glob('../../../tests/research/fixtures/pilot/pilot_view.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>
const FIXTURE = Object.values(fixtures)[0] as PilotView

const BASELINE = 'eq-baseline-spy-weekly'
const CHALLENGER = 'eq-challenger-spy-reversal'
const OBSERVATION = 'qsl-observation'

function view(changes: Partial<PilotView> = {}): PilotView {
  return { ...structuredClone(FIXTURE), ...changes } as PilotView
}

type Call = { url: string; init?: RequestInit }

function stubFetch(body: PilotView, post: { status: number; json: unknown } = { status: 202, json: {} }) {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init })
      const isPost = init?.method === 'POST'
      return new Response(JSON.stringify(isPost ? post.json : body), {
        status: isPost ? post.status : 200,
        headers: { 'content-type': 'application/json' },
      })
    }),
  )
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the pilot view mirror', () => {
  it('names every key the server sends', () => {
    expect(FIXTURE.schema).toBe('trex-pilot-view/1')
    expect([...PILOT_VIEW_KEYS].sort()).toEqual(Object.keys(FIXTURE).sort())
    for (const row of FIXTURE.programs ?? []) {
      expect([...PILOT_ROW_KEYS].sort()).toEqual(Object.keys(row).sort())
      if (row.registration) expect([...KEYS.PilotRegistration].sort()).toEqual(Object.keys(row.registration).sort())
      if (row.usage) expect([...KEYS.BudgetUsage].sort()).toEqual(Object.keys(row.usage).sort())
    }
  })

  it('builds the exact seal_registration command body', () => {
    const body = sealCommandBody(BASELINE, 'a'.repeat(64), '5f0c2a8e-1b3d-4c6e-9a7b-2d4e6f8a0b1c', new Date('2026-10-22T14:00:00.700Z'))
    expect(body).toEqual({
      schema: 'trex-command/1',
      idempotency_key: '5f0c2a8e-1b3d-4c6e-9a7b-2d4e6f8a0b1c',
      kind: 'seal_registration',
      args: { program_id: BASELINE, registration_sha256: 'a'.repeat(64) },
      issued_at: '2026-10-22T14:00:00Z',
      expires_at: '2026-10-22T14:05:00Z',
    })
    expect([...KEYS.CommandBody].sort()).toEqual(Object.keys(body).sort())
    expect(() => sealCommandBody(BASELINE, 'A'.repeat(64), '5f0c2a8e-1b3d-4c6e-9a7b-2d4e6f8a0b1c', new Date())).toThrow(RangeError)
    expect(() => sealCommandBody(BASELINE, 'a'.repeat(64), 'not-a-uuid', new Date())).toThrow(RangeError)
  })
})

describe('#/pilot', () => {
  it('lists the three programs in role order with honest states and no auto action', async () => {
    const calls = stubFetch(view())
    render(<PilotPanel />)
    const cards = await screen.findAllByRole('article')
    expect(cards.map((c) => c.getAttribute('data-program'))).toEqual([OBSERVATION, BASELINE, CHALLENGER])
    expect(within(cards[0]).getByText(/Sealed by owner@example\.com/)).toBeTruthy()
    expect(within(cards[1]).getByText('Draft: cannot run until the owner seals it.')).toBeTruthy()
    // a zero budget reads as a decision, not as a used-up day
    expect(within(cards[0]).getByText('No pilot request budget: no pilot job runs under this program.')).toBeTruthy()
    expect(within(cards[0]).queryByText(/budget is used up/)).toBeNull()
    expect(screen.getByText(/Paper research only/)).toBeTruthy()
    expect(screen.getByText(/never authorizes live trading/)).toBeTruthy()
    // only reads: the page never posts a command by itself
    expect(calls.every((c) => (c.init?.method ?? 'GET') === 'GET')).toBe(true)
    expect(calls[0].url).toBe('api/pilot')
  })

  it('shows unknown usage as unavailable with its reason, never as zero', async () => {
    stubFetch(view())
    render(<PilotPanel />)
    const card = (await screen.findAllByRole('article'))[2]
    expect(within(card).getByText(/Usage unavailable/)).toBeTruthy()
    expect(within(card).getByText(/budget_usage_unknown:unreadable/)).toBeTruthy()
    expect(within(card).queryByText(/0 of 20/)).toBeNull()
    expect(within(card).getByText(/Today's usage is unknown/)).toBeTruthy()
  })

  it('shows known usage against each budget', async () => {
    stubFetch(view())
    render(<PilotPanel />)
    const card = (await screen.findAllByRole('article'))[1]
    expect(within(card).getByText('3 of 20 requests')).toBeTruthy()
    expect(within(card).queryByText(/No pilot .* budget/)).toBeNull()
    expect(within(card).getByText('2 of 10 compute minutes')).toBeTruthy()
    expect(within(card).getByText('2.0 KiB of 64 MiB storage')).toBeTruthy()
  })

  it('renders an unreadable pilot as unavailable, with no program list', async () => {
    stubFetch(view({ status: 'unavailable', unavailable_reason: 'pilot_dir_unreadable', programs: null, roster_problems: null } as Partial<PilotView>))
    render(<PilotPanel />)
    expect((await screen.findByRole('alert')).textContent).toContain('pilot_dir_unreadable')
    expect(screen.queryAllByRole('article')).toHaveLength(0)
  })

  it('reports a failed fetch without inventing a roster', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>sign in</html>', { status: 403, headers: { 'content-type': 'text/html' } })))
    render(<PilotPanel />)
    expect((await screen.findByRole('alert')).textContent).toMatch(/unavailable/i)
    expect(screen.queryAllByRole('article')).toHaveLength(0)
  })

  it('warns when the server has no owner list', async () => {
    stubFetch(view({ owners_configured: false }))
    render(<PilotPanel />)
    expect(await screen.findByText(/No owner list is configured/)).toBeTruthy()
  })
})

describe('#/pilot/:programId', () => {
  it('shows the full registration with its provenance and review hash', async () => {
    stubFetch(view())
    render(<PilotPanel programId={BASELINE} />)
    const row = FIXTURE.programs!.find((p) => p.program_id === BASELINE)!
    expect(await screen.findByRole('heading', { name: row.registration!.title })).toBeTruthy()
    expect(screen.getByText(row.registration!.hypothesis)).toBeTruthy()
    expect(screen.getByText(row.registration!.data_sources[1].provenance)).toBeTruthy()
    expect(screen.getAllByText(row.review_sha256!).length).toBeGreaterThan(0)
    expect(screen.getByRole('link', { name: 'All pilot programs' }).getAttribute('href')).toBe('#/pilot')
  })

  it('explains the loopback seal while the browser seal is not enabled', async () => {
    stubFetch(view({ seal_command_available: false }))
    render(<PilotPanel programId={BASELINE} />)
    expect(await screen.findByText(/not enabled on this server yet/)).toBeTruthy()
    expect(screen.getByText(/python -m tree_options\.research\.pilot seal --program-id eq-baseline-spy-weekly/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Seal registration' })).toBeNull()
  })

  it('seals only after the owner confirms the reviewed hash, with that hash', async () => {
    const calls = stubFetch(view({ seal_command_available: true }), {
      status: 202,
      json: { schema: 'trex-command-result/1', status: 'accepted', reason: null, kind: 'seal_registration' },
    })
    render(<PilotPanel programId={BASELINE} />)
    const button = (await screen.findByRole('button', { name: 'Seal registration' })) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.click(button)
    expect(calls.filter((c) => c.init?.method === 'POST')).toHaveLength(0)
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed this registration/ }))
    expect(button.disabled).toBe(false)
    fireEvent.click(button)
    expect(await screen.findByText(/Seal queued/)).toBeTruthy()
    expect(button.disabled).toBe(true) // one seal command per visit
    fireEvent.click(button)
    const posts = calls.filter((c) => c.init?.method === 'POST')
    expect(posts).toHaveLength(1)
    expect(posts[0].url).toBe('api/commands')
    const sent = JSON.parse(String(posts[0].init?.body))
    const row = FIXTURE.programs!.find((p) => p.program_id === BASELINE)!
    expect(sent.kind).toBe('seal_registration')
    expect(sent.args).toEqual({ program_id: BASELINE, registration_sha256: row.review_sha256 })
    expect(sent.idempotency_key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
    expect('issued_by' in sent).toBe(false)
  })

  it('shows a refused seal with its reason', async () => {
    stubFetch(view({ seal_command_available: true }), {
      status: 409,
      json: { schema: 'trex-command-result/1', status: 'refused', reason: 'sealer_not_owner', kind: 'seal_registration' },
    })
    render(<PilotPanel programId={BASELINE} />)
    fireEvent.click(await screen.findByRole('checkbox', { name: /I reviewed this registration/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Seal registration' }))
    expect((await screen.findByText(/Seal refused/)).textContent).toContain('sealer_not_owner')
  })

  it('keeps a challenger unsealable until its baseline is sealed', async () => {
    stubFetch(view({ seal_command_available: true }))
    render(<PilotPanel programId={CHALLENGER} />)
    expect(await screen.findByText(/Seal eq-baseline-spy-weekly first/)).toBeTruthy()
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed this registration/ }))
    expect((screen.getByRole('button', { name: 'Seal registration' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('offers no seal for a sealed program', async () => {
    stubFetch(view({ seal_command_available: true }))
    render(<PilotPanel programId={OBSERVATION} />)
    expect(await screen.findByText(/Sealed by owner@example\.com/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Seal registration' })).toBeNull()
    expect(screen.getByText(/Sealed registrations are never edited/)).toBeTruthy()
  })

  it('says so when the program is not in the pilot', async () => {
    stubFetch(view())
    render(<PilotPanel programId="eq-unknown" />)
    expect(await screen.findByText(/not in the pilot/)).toBeTruthy()
  })
})
