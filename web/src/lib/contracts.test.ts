import { describe, expect, it } from 'vitest'
import {
  ACCEPTANCE_THRESHOLDS,
  ACTIVITY_SOURCES,
  API_PATHS,
  CONTROL_LABELS,
  DRILL_IDS,
  KEYS,
  PILOT_BUDGET_CEILINGS,
  PILOT_ROLES,
  PRIMARY_CONTROL,
  WAVE3_REASONS,
  activityQuery,
  buildCommandBody,
  isKnown,
  secondsLeft,
  type AcceptanceView,
  type PendingApproval,
  type TodayView,
} from './contracts'
import {
  DEFAULT_DESTINATION,
  DESTINATIONS,
  ROUTES,
  advancedFor,
  hashFor,
  resolveHash,
  type LegacyView,
} from './routes'
import { parseHash, serialize, type Route } from './router'

// The shared fixtures are the contract between this mirror and the Python
// models (tests/contracts/test_wave3_contracts.py round-trips the same files).
const wave3 = import.meta.glob('../../../tests/contracts/fixtures/wave3/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>
const wave2 = import.meta.glob('../../../tests/contracts/fixtures/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>

type Json = Record<string, unknown>

function fixture(name: string, from: Record<string, unknown> = wave3): Json {
  const hit = Object.entries(from).find(([path]) => path.endsWith(`/${name}`))
  if (!hit) throw new Error(`fixture ${name} not found`)
  return structuredClone(hit[1]) as Json
}

function expectKeys(value: unknown, keys: readonly string[], where: string): void {
  expect(value, where).toBeTypeOf('object')
  expect(Object.keys(value as Json).sort(), where).toEqual([...keys].sort())
}

function at(value: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((node, key) => (node as Json)[key], value)
}

describe('key lists match the shared fixtures', () => {
  it('trex-today/1 (known and unavailable)', () => {
    for (const name of ['today.json', 'today_unavailable.json']) {
      const doc = fixture(name)
      const checks: [string, readonly string[]][] = [
        ['', KEYS.TodayView],
        ['session', KEYS.SessionState],
        ['desk_owner', KEYS.DeskOwnerHealth],
        ['gateway', KEYS.GatewayHealth],
        ['controls', KEYS.Controls],
        ['controls.pause_entries', KEYS.KillSwitchState],
        ['controls.halt', KEYS.KillSwitchState],
        ['controls.flatten', KEYS.KillSwitchState],
        ['controls.queued', KEYS.QueuedCommands],
        ['approvals', KEYS.PendingApprovals],
        ['positions', KEYS.PositionsSummary],
        ['positions.holdings', KEYS.HoldingsSummary],
        ['positions.risk', KEYS.RiskSummary],
        ['acceptance', KEYS.AcceptanceSummary],
        ['next_events', KEYS.NextEvents],
      ]
      for (const [path, keys] of checks) expectKeys(path ? at(doc, path) : doc, keys, `${name}:${path}`)
      for (const blocker of doc.blockers as Json[]) expectKeys(blocker, KEYS.HumanBlocker, `${name}:blocker`)
    }
    const today = fixture('today.json')
    expectKeys(at(today, 'controls.queued.items.0'), KEYS.QueuedCommand, 'queued item')
    expectKeys(at(today, 'approvals.items.0'), KEYS.PendingApproval, 'approval')
    expectKeys(at(today, 'approvals.items.0.approve_args'), KEYS.ApproveEntryArgs, 'approve_args')
    for (const event of at(today, 'next_events.items') as Json[]) expectKeys(event, KEYS.ScheduledEvent, 'event')
  })

  it('trex-activity/1', () => {
    const page = fixture('activity.json')
    expectKeys(page, KEYS.ActivityPage, 'page')
    for (const item of page.items as Json[]) {
      expectKeys(item, KEYS.ActivityItem, String(item.id))
      if (item.actor !== null) expectKeys(item.actor, KEYS.ActivityActor, 'actor')
    }
    for (const source of page.sources as Json[]) expectKeys(source, KEYS.ActivitySourceStatus, 'source')
    expect((page.sources as Json[]).map((s) => s.source)).toEqual([...ACTIVITY_SOURCES])
  })

  it('trex-acceptance/1 and its view', () => {
    const view = fixture('acceptance_view.json')
    expectKeys(view, KEYS.AcceptanceView, 'view')
    const doc = view.document as Json
    expectKeys(doc, KEYS.AcceptanceDocument, 'document')
    const lists: [string, readonly string[]][] = [
      ['calendar_window', KEYS.CalendarSession],
      ['sessions', KEYS.SessionRecord],
      ['lifecycles', KEYS.LifecycleRecord],
      ['drills', KEYS.DrillRecord],
      ['session_results', KEYS.SessionResult],
      ['lifecycle_results', KEYS.LifecycleResult],
      ['drill_results', KEYS.DrillResult],
    ]
    for (const [name, keys] of lists) {
      expect((doc[name] as Json[]).length, name).toBeGreaterThan(0)
      for (const row of doc[name] as Json[]) expectKeys(row, keys, name)
    }
    expectKeys(doc.streak, KEYS.StreakSummary, 'streak')
    expectKeys(at(doc, 'per_kind.option'), KEYS.KindCounters, 'option')
    expectKeys(at(doc, 'per_kind.equity'), KEYS.KindCounters, 'equity')
    expect((doc.drills as Json[]).map((d) => d.id)).toEqual([...DRILL_IDS])
    expect(doc.live_authorized).toBe(false)
    expect(view.live_authorized).toBe(false)
  })

  it('trex-pilot-registration/1 and usage', () => {
    for (const name of [
      'pilot_registration_observation.json',
      'pilot_registration_baseline_draft.json',
      'pilot_registration_challenger_sealed.json',
    ]) {
      expectKeys(fixture(name), KEYS.PilotRegistration, name)
    }
    expectKeys(fixture('pilot_usage.json'), KEYS.BudgetUsage, 'usage')
  })

  it('Wave 2 mirrors: proposals, portfolio, holdings, commands', () => {
    const list = fixture('wave2_proposal_list.json')
    expectKeys(list, KEYS.ProposalList, 'proposal list')
    const row = (list.proposals as Json[])[0]
    expectKeys(row, KEYS.ProposalRow, 'proposal row')
    expectKeys(row.proposal, KEYS.EntryProposalDoc, 'proposal doc')
    expectKeys(fixture('wave2_portfolio_unavailable.json'), KEYS.PortfolioView, 'portfolio')
    expectKeys(fixture('holding.json', wave2), KEYS.Holding, 'holding')
    expectKeys(fixture('command_result_applied.json', wave2), KEYS.CommandResult, 'command result')
    const stored = fixture('command_pause_entries.json', wave2)
    delete stored.issued_by // stamped by the backend, never sent by a client
    expectKeys(stored, KEYS.CommandBody, 'command body')
    expectKeys(JSON.parse(fixture('family_command_assertion.json').body as string), KEYS.CommandBody, 'golden body')
  })
})

describe('vocabulary and constants match the Python modules', () => {
  const vocab = fixture('vocabulary.json')
  it('lists', () => {
    expect([...WAVE3_REASONS]).toEqual(vocab.wave3_reasons)
    expect([...DRILL_IDS]).toEqual(vocab.drill_ids)
    expect([...ACTIVITY_SOURCES]).toEqual(vocab.activity_sources)
    expect([...PILOT_ROLES]).toEqual(vocab.pilot_roles)
    expect(PILOT_BUDGET_CEILINGS).toEqual(vocab.pilot_budget_ceilings)
    expect(ACCEPTANCE_THRESHOLDS).toEqual(vocab.acceptance_thresholds)
    expect(API_PATHS).toEqual(vocab.api_paths)
  })
  it('control labels; the pause is primary and HALT says it stops exits', () => {
    const labels = fixture('control_labels.json')
    expect({ primary_control: PRIMARY_CONTROL, labels: CONTROL_LABELS }).toEqual(labels)
    expect(CONTROL_LABELS.halt).toBe('Emergency stop (also stops exits)')
  })
})

describe('helpers', () => {
  it('isKnown narrows on status', () => {
    const today = fixture('today.json') as unknown as TodayView
    const down = fixture('today_unavailable.json') as unknown as TodayView
    expect(isKnown(today.session)).toBe(true)
    if (isKnown(today.session)) expect(today.session.opens_at.endsWith('Z')).toBe(true)
    expect(isKnown(down.session)).toBe(false)
    expect(down.session.unavailable_reason).toBe('calendar_unreadable')
    const view = fixture('acceptance_view.json') as unknown as AcceptanceView
    if (view.status === 'ok') expect(view.document.verdict).toBe('not_met')
  })

  it('counts an approval down from receipt, never past zero', () => {
    const approval = { seconds_to_deadline: 480 } as PendingApproval
    const received = 1_000_000
    expect(secondsLeft(approval, received, received)).toBe(480)
    expect(secondsLeft(approval, received, received + 125_400)).toBe(355)
    expect(secondsLeft(approval, received, received - 5_000)).toBe(480)
    expect(secondsLeft(approval, received, received + 600_000)).toBe(0)
  })

  it('builds the exact body bytes the golden assertion signs', () => {
    const golden = fixture('family_command_assertion.json')
    const body = buildCommandBody(
      'pause_entries',
      { reason: 'pausing from my phone' },
      '5f0c2a8e-1b3d-4c6e-9a7b-2d4e6f8a0b1c',
      new Date('2026-10-22T14:00:00.700Z'),
    )
    expect(JSON.stringify(body)).toBe(golden.body)
    expect(() => buildCommandBody('halt', {}, '5f0c2a8e-1b3d-4c6e-9a7b-2d4e6f8a0b1c', new Date(), 301)).toThrow(RangeError)
    expect(() => buildCommandBody('halt', {}, '5F0C2A8E-1B3D-4C6E-9A7B-2D4E6F8A0B1C', new Date())).toThrow(RangeError)
  })

  it('activity query strings', () => {
    expect(activityQuery({})).toBe('/api/activity')
    expect(activityQuery({ limit: 20, cursor: 'abc', sources: ['command', 'drill'] })).toBe(
      '/api/activity?limit=20&cursor=abc&source=command&source=drill',
    )
  })
})

describe('trex-family-command/1 golden (family-apps signing, rebuilt with WebCrypto)', () => {
  const CLAIM_ORDER = [
    'v',
    'sub',
    'aud',
    'iat',
    'exp',
    'method',
    'path',
    'ingress',
    'origin',
    'request_id',
    'request_sha256',
    'jti',
  ]
  const b64url = (bytes: Uint8Array) =>
    btoa(String.fromCharCode(...bytes))
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '')
  const hex = (bytes: Uint8Array) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')

  it('JSON.stringify in claim order + HMAC-SHA256 reproduces the token', async () => {
    const golden = fixture('family_command_assertion.json')
    const claims = golden.claims as Json
    const ordered = Object.fromEntries(CLAIM_ORDER.map((name) => [name, claims[name]]))
    expect(Object.keys(claims).sort()).toEqual([...CLAIM_ORDER].sort())
    const enc = new TextEncoder()
    const payload = b64url(enc.encode(JSON.stringify(ordered)))
    const raw = Uint8Array.from(atob(golden.key_b64 as string), (ch) => ch.charCodeAt(0))
    const key = await crypto.subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
    const signature = b64url(new Uint8Array(await crypto.subtle.sign('HMAC', key, enc.encode(payload))))
    expect(`${payload}.${signature}`).toBe(golden.token)
    const digest = hex(new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(golden.body as string))))
    expect(digest).toBe(claims.request_sha256)
    expect(claims.request_id).toBe(JSON.parse(golden.body as string).idempotency_key)
    expect(claims.exp).toBe((claims.iat as number) + 60)
    expect(golden.portal_path).toBe(`/trex${golden.path as string}`)
  })
})

describe('routes', () => {
  it('five destinations, Today first and default', () => {
    expect(DESTINATIONS.map((d) => [d.id, d.label, d.hash])).toEqual([
      ['today', 'Today', '#/today'],
      ['experiments', 'Experiments', '#/experiments'],
      ['trade-review', 'Trade review', '#/trade-review'],
      ['portfolio', 'Portfolio', '#/portfolio'],
      ['activity', 'Activity & settings', '#/activity'],
    ])
    expect(DEFAULT_DESTINATION).toBe('today')
    for (const d of DESTINATIONS) {
      const resolved = resolveHash(d.hash)
      expect([resolved.entry.group, resolved.destination, resolved.fallback]).toEqual(['destination', d.id, false])
    }
  })

  it('the root opens Today; unknown hashes fall back to Today', () => {
    for (const hash of ['', '#', '#/']) {
      expect(resolveHash(hash)).toMatchObject({ destination: 'today', fallback: false })
    }
    for (const hash of ['#/discoveryism', '#/nope', '#/trade-review/xyz']) {
      expect(resolveHash(hash)).toMatchObject({ destination: 'today', fallback: true })
    }
  })

  it('every route id is unique and every example resolves to its own entry', () => {
    expect(new Set(ROUTES.map((r) => r.id)).size).toBe(ROUTES.length)
    for (const route of ROUTES) expect(resolveHash(route.example).entry.id, route.id).toBe(route.id)
  })

  it('every pre-Wave-3 view stays reachable as an alias', () => {
    const legacy: Route[] = [
      { view: 'list' },
      { view: 'plan', id: 'spy put/spread' },
      { view: 'discover' },
      { view: 'stats' },
      { view: 'market' },
      { view: 'symbol', id: 'SPY' },
      { view: 'research' },
      { view: 'action-model' },
      { view: 'trade-floor' },
      { view: 'quant' },
      { view: 'workspace' },
      { view: 'experiments' },
      { view: 'experiments', id: 'qsl:observation' },
    ]
    const views = new Set<LegacyView>(ROUTES.flatMap((r) => (r.legacyView ? [r.legacyView] : [])))
    for (const route of legacy) {
      expect(views.has(route.view), route.view).toBe(true)
      if (route.view === 'list') continue // '#/' opens Today now; Plans is '#/plans'
      const hash = serialize(route)
      expect(parseHash(hash)).toEqual(route)
      expect(resolveHash(hash).entry.legacyView, hash).toBe(route.view)
      expect(resolveHash(hash).fallback).toBe(false)
    }
    expect(resolveHash('#/plans').entry.legacyView).toBe('list')
    for (const route of ROUTES) {
      if (route.legacyView && route.id !== 'plans') expect(parseHash(route.example).view, route.id).toBe(route.legacyView)
    }
  })

  it('advanced views are grouped under a destination', () => {
    expect(advancedFor('experiments').map((r) => r.id)).toEqual([
      'discover',
      'research',
      'action-model',
      'quant',
      'workspace',
      'trade-floor',
    ])
    expect(advancedFor('portfolio').map((r) => r.id)).toEqual(['plans', 'stats'])
    expect(advancedFor('trade-review').map((r) => r.id)).toEqual(['market'])
    expect(advancedFor('today')).toEqual([])
  })

  it('hashFor round-trips parameters', () => {
    expect(hashFor('plan', 'a b')).toBe('#/plan/a%20b')
    expect(resolveHash(hashFor('plan', 'a b')).param).toBe('a b')
    const id = 'f'.repeat(64)
    expect(resolveHash(hashFor('trade-review-proposal', id))).toMatchObject({ param: id, destination: 'trade-review' })
    expect(hashFor('activity-acceptance')).toBe('#/activity/acceptance')
    expect(() => hashFor('plan')).toThrow()
  })
})
