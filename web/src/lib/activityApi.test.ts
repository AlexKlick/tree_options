import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ACTIVITY_PAGE_SIZE,
  COMMAND_STATUS_LABELS,
  DRILL_LABELS,
  INGRESS_LABELS,
  REASON_WORDS,
  SOURCE_LABELS,
  actorText,
  appendPage,
  getAcceptance,
  getActivity,
  getToday,
  onboardingSteps,
  reasonText,
  reasonWords,
  relativePath,
  toggleSource,
} from './activityApi'
import {
  ACTIVITY_SOURCES,
  API_PATHS,
  DRILL_IDS,
  WAVE3_REASONS,
  type ActivityItem,
  type ActivityPage,
  type TodayView,
} from './contracts'

// The shared fixtures pin the wire shapes (tests/contracts/fixtures/wave3/).
const wave3 = import.meta.glob('../../../tests/contracts/fixtures/wave3/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>
function fixture<T>(name: string): T {
  const doc = wave3[`../../../tests/contracts/fixtures/wave3/${name}`]
  if (doc === undefined) throw new Error(`fixture ${name} missing`)
  return structuredClone(doc) as T
}
const vocabulary = fixture<Record<string, string[]>>('vocabulary.json')

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
}

afterEach(() => vi.unstubAllGlobals())

describe('fetchers', () => {
  it('reads every contract path relative to the mount (never a leading slash)', async () => {
    const urls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      urls.push(url)
      return jsonResponse({})
    }))
    await getToday()
    await getAcceptance()
    await getActivity({ limit: ACTIVITY_PAGE_SIZE, cursor: 'abc_-1', sources: ['command', 'drill'] })
    await getActivity({})
    expect(urls).toEqual([
      API_PATHS.today.slice(1),
      API_PATHS.acceptance.slice(1),
      `${API_PATHS.activity.slice(1)}?limit=${ACTIVITY_PAGE_SIZE}&cursor=abc_-1&source=command&source=drill`,
      API_PATHS.activity.slice(1),
    ])
    for (const url of urls) expect(url.startsWith('/')).toBe(false)
  })

  it('refuses a non-JSON answer (the portal sign-in page is not data)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>sign in</html>', {
      status: 200,
      headers: { 'content-type': 'text/html' },
    })))
    await expect(getToday()).rejects.toThrow(/api\/today/)
  })

  it('strips only the leading slashes of a path', () => {
    for (const path of Object.values(API_PATHS)) {
      expect(relativePath(path)).toBe(path.slice(1))
    }
    expect(relativePath('api/x')).toBe('api/x')
  })

  it('pages at the contract default of 50', () => {
    expect(ACTIVITY_PAGE_SIZE).toBe(50)
  })
})

describe('vocabulary words', () => {
  it('has words for every reason a Wave 3 view or acceptance can show', () => {
    const codes = [
      ...WAVE3_REASONS,
      ...vocabulary.session_reasons,
      ...vocabulary.lifecycle_reasons,
      ...vocabulary.drill_reasons,
      ...vocabulary.verdict_reasons,
    ]
    const missing = codes.filter((code) => !(code in REASON_WORDS))
    expect(missing).toEqual([])
  })

  it('keeps the machine code beside the words and never drops an unknown code', () => {
    expect(reasonText('holdings_missing')).toBe(`${REASON_WORDS.holdings_missing} (holdings_missing)`)
    expect(reasonWords('some_new_code')).toBe('some new code')
    expect(reasonText('some_new_code')).toBe('some new code (some_new_code)')
  })

  it('names the drill a verdict reason points at', () => {
    for (const id of DRILL_IDS) {
      expect(reasonWords(`drill_not_passed:${id}`)).toContain(DRILL_LABELS[id])
    }
  })

  it('labels every source, ingress and command status', () => {
    expect(Object.keys(SOURCE_LABELS)).toEqual([...ACTIVITY_SOURCES])
    expect(Object.keys(INGRESS_LABELS).sort()).toEqual(['family', 'loopback', 'system'])
    expect(Object.keys(COMMAND_STATUS_LABELS).sort()).toEqual(
      ['accepted', 'applied', 'duplicate', 'failed', 'refused'],
    )
    expect(Object.keys(DRILL_LABELS)).toEqual([...DRILL_IDS])
  })
})

describe('activity feed helpers', () => {
  const page = fixture<ActivityPage>('activity.json')

  it('says who acted and through which door, or that it was not recorded', () => {
    const [family, desk, , audit] = page.items
    expect(actorText(family)).toBe(`owner@example.com via ${INGRESS_LABELS.family}`)
    expect(actorText(desk)).toBe(`trex-desk via ${INGRESS_LABELS.system}`)
    expect(actorText(audit)).toBe(`Actor not recorded: ${reasonText('actor_not_recorded')}`)
  })

  it('appends an older page without repeating an item', () => {
    const older: ActivityItem = { ...page.items[3], id: 'audit:kill-files:older', at: '2026-10-22T13:00:00Z' }
    const merged = appendPage(page.items, { ...page, items: [page.items[3], older] })
    expect(merged.map((i) => i.id)).toEqual([...page.items.map((i) => i.id), older.id])
  })

  it('treats no selected source as every source', () => {
    expect(toggleSource([], 'command')).toEqual(['command'])
    expect(toggleSource(['command'], 'drill')).toEqual(['command', 'drill'])
    expect(toggleSource(['drill', 'command'], 'audit')).toEqual(['command', 'audit', 'drill'])
    expect(toggleSource(['command'], 'command')).toEqual([])
    const all = toggleSource(toggleSource(toggleSource(['command'], 'desk_event'), 'audit'), 'drill')
    expect(all).toEqual([])
  })
})

describe('onboarding checklist from /api/today', () => {
  const today = fixture<TodayView>('today.json')
  const unavailable = fixture<TodayView>('today_unavailable.json')
  const byId = (view: TodayView) => Object.fromEntries(onboardingSteps(view).map((s) => [s.id, s]))

  it('lists the five first-run steps in order', () => {
    expect(onboardingSteps(today).map((s) => s.id)).toEqual([
      'gateway',
      'heartbeat',
      'profile',
      'family_key',
      'first_approval',
    ])
  })

  it('marks every step done on a healthy desk with a counted paper trade', () => {
    const steps = byId(today)
    for (const id of ['gateway', 'heartbeat', 'profile', 'family_key', 'first_approval']) {
      expect([id, steps[id].state]).toEqual([id, 'done'])
    }
    expect(steps.profile.detail).toContain(today.approvals.items![0].approve_args.profile_digest.slice(0, 12))
  })

  it('never marks an unknown fact done', () => {
    const steps = byId(unavailable)
    expect(steps.gateway.state).toBe('unknown')
    expect(steps.gateway.detail).toContain('gateway_watch_not_reporting')
    expect(steps.heartbeat.state).toBe('unknown')
    expect(steps.heartbeat.detail).toContain('heartbeat_absent')
    expect(steps.profile.state).toBe('unknown')
    expect(steps.first_approval.state).toBe('unknown')
    expect(steps.first_approval.detail).toContain('acceptance_missing')
    expect(steps.first_approval.detail).toContain('holdings_missing')
  })

  it('asks for a login or for 2FA by the gateway state', () => {
    const login = byId({ ...today, gateway: { ...today.gateway, status: 'needs_login', detail: 'login dialog' } as TodayView['gateway'] })
    expect(login.gateway.state).toBe('todo')
    expect(login.gateway.next).toMatch(/Paper Trading/)
    const twofa = byId({ ...today, gateway: { ...today.gateway, status: 'needs_2fa', detail: '2FA prompt' } as TodayView['gateway'] })
    expect(twofa.gateway.state).toBe('todo')
    expect(twofa.gateway.next).toMatch(/IBKR Mobile/)
  })

  it('reports a stale or stopped desk owner as not ready', () => {
    const stale = byId({
      ...today,
      desk_owner: { ...today.desk_owner, status: 'stale', heartbeat_age_s: 300, exit_owner_ready: false } as TodayView['desk_owner'],
    })
    expect(stale.heartbeat.state).toBe('todo')
    expect(stale.heartbeat.detail).toContain('5m')
    expect(stale.heartbeat.detail).toContain(`${today.desk_owner.stale_after_s}s`)
  })

  it('reads a profile blocker as to do, and no evidence as unknown', () => {
    const blocked = byId({
      ...today,
      blockers: [{ code: 'profile_absent', scope: 'entries', severity: 'critical', message: 'No capital profile.', next_step: 'Write profile.json.' }],
    })
    expect(blocked.profile.state).toBe('todo')
    expect(blocked.profile.detail).toContain('No capital profile.')
    const quiet = byId({
      ...today,
      approvals: { ...today.approvals, items: [] } as TodayView['approvals'],
      acceptance: { ...today.acceptance, option_lifecycles: 0, equity_lifecycles: 0 } as TodayView['acceptance'],
      positions: {
        ...today.positions,
        holdings: { ...today.positions.holdings, option_positions: 0, equity_positions: 0 } as TodayView['positions']['holdings'],
      },
    })
    expect(quiet.profile.state).toBe('unknown')
    expect(quiet.first_approval.state).toBe('todo')
    expect(quiet.first_approval.href).toBe('#/today')
  })

  it('says the proposals cannot be read rather than that none is waiting', () => {
    const steps = byId(unavailable)
    expect(steps.profile.detail).toContain('proposals_dir_unreadable')
    expect(steps.profile.detail).not.toContain('none is waiting')
  })

  it('reads family_commands_disabled as the family key not installed', () => {
    const steps = byId({
      ...today,
      blockers: [{ code: 'family_commands_disabled', scope: 'commands', severity: 'warning', message: 'Phone commands are off.', next_step: 'Install the key.' }],
    })
    expect(steps.family_key.state).toBe('todo')
    expect(steps.family_key.detail).toContain('Phone commands are off.')
    expect(steps.family_key.next).toContain('Install the key.')
  })

  it('points a waiting proposal at its review page', () => {
    const steps = byId({
      ...today,
      acceptance: { ...today.acceptance, option_lifecycles: 0, equity_lifecycles: 0 } as TodayView['acceptance'],
      positions: {
        ...today.positions,
        holdings: { ...today.positions.holdings, option_positions: 0, equity_positions: 0 } as TodayView['positions']['holdings'],
      },
    })
    expect(steps.first_approval.state).toBe('todo')
    expect(steps.first_approval.href).toBe(`#/trade-review/${today.approvals.items![0].proposal_id}`)
  })
})
