import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ActionModelPage } from './ActionModelPage'
import { getActionModelExample, getAutomation, getHistoricalReplays, getIntradayGraphs, getLabScoreboard, getLongRun, getPlans, getPortfolioScenarios, getSupervisedDesk, postAutomationAction, postSupervisedControl } from '../lib/api'
import type { AccountExposure, LongRunView, PlansResponse } from '../lib/types'

vi.mock('../lib/api', () => ({
  getActionModelExample: vi.fn(),
  getPlans: vi.fn(),
  getHistoricalReplays: vi.fn(),
  getIntradayGraphs: vi.fn(),
  getPortfolioScenarios: vi.fn(),
  getSupervisedDesk: vi.fn(),
  getLabScoreboard: vi.fn(),
  getLongRun: vi.fn(),
  getAutomation: vi.fn(),
  postAutomationAction: vi.fn(),
  postSupervisedControl: vi.fn(),
  getGateway: () => new Promise(() => {}),
  getExitMachine: () => new Promise(() => {}),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('shows proposal status and inspects guards without a trade control', async () => {
  vi.mocked(getActionModelExample).mockResolvedValue({
    plan: {
      plan_id: 'example.research-to-paper', revision: 1,
      goal: 'Compare a version', state: 'proposed',
      artifact_status: 'synthetic_design_example', execution_authorized: false,
      source_repository_head: 'c'.repeat(40), artifacts: [],
      nodes: [{
        id: 'N01', label: 'Resolve inputs', operation: 'research.resolve_inputs',
        operation_version: '0.1', owner_role: 'data_service', effect_class: 'read',
        target_environment: 'research', dependencies: [], inputs: {},
        outputs: { snapshot: 'DataSnapshot' }, required_guards: ['input_hash_match'],
        required_receipts: ['DataSnapshot'], postcondition: 'snapshot_resolved',
      }],
    },
    receipt: { valid_structure: true, node_count: 1, dependency_count: 0,
      execution_authorized: false, broker_contacted: false, scope: 'fixture only' },
  })
  vi.mocked(getPlans).mockResolvedValue({ account: null } as Awaited<ReturnType<typeof getPlans>>)
  vi.mocked(getHistoricalReplays).mockResolvedValue({
    schema: 'desk-historical-replay-list/1', execution_enabled: false, reports: [{
      id: 'replay-one', label: 'exploratory',
      spec: { start: '2025-01-01', end: '2025-06-30', names: ['AVGO'], entry_dte: [30, 90], haircut: 0.01, max_loss: 300 },
      counts: { attempted: 21, evaluable_within_trade_cap: 1, missing_decision_spot_or_options: 6, missing_exit_or_entry_bar: 1, over_trade_loss_cap: 18 },
      by_structure: {}, by_variant: { 'xsmom_top3/put_credit': { trades: 1, wins: 1, win_rate: 1, mean_pnl: 41.19, worst_pnl: 41.19 } },
      provenance: { sources: [] }, limitations: ['modeled prices'],
    }],
  })
  vi.mocked(getIntradayGraphs).mockResolvedValue({
    schema: 'desk-intraday-graph-list/1', execution_enabled: false, reports: [{
      id: 'sample/rolling-put-credit', policy: 'put_credit', source_sha256: 'd'.repeat(64),
      requested_contracts: 72, captured_contracts: 72, traded_minute_bars: 172748,
      limitations: ['trade bars are not executable quotes'], windows: [{
        start: '2026-05-26', end: '2026-08-25', sessions: 64, scheduled_snapshots: 512,
        potential_trades: 100, entered: 3, modeled_wins: 1, modeled_losses: 2,
        open_at_end: 0, closed_capital_proxy: '4998', minimum_closed_capital_proxy: '4970',
        peak_open_loss_reserved: '300',
      }],
    }],
  })
  vi.mocked(getPortfolioScenarios).mockResolvedValue({
    schema: 'desk-portfolio-scenario-list/1', execution_enabled: false, reports: [{
      id: 'portfolio-one', label: 'exploratory',
      spec: { intended_capital: '5000', max_trade_loss: '300', max_open_loss: '1500' },
      variants: { 'xsmom_top3/put_credit': { considered: 2, admitted: 1,
        skipped: { trade_cap: 0, open_cap: 1, capital: 0 },
        peak_open_loss_reserved: '150', closed_pnl: '41',
        ending_closed_capital: '5041', minimum_closed_capital: '5000' } },
      provenance: { replay_sha256: 'a'.repeat(64), code_head: 'b'.repeat(40), code_dirty: false },
      limitations: ['modeled prices'],
    }],
  })
  vi.mocked(getSupervisedDesk).mockResolvedValue({
    schema: 'desk-cli-status/1', at: '2026-09-28T15:00:00+00:00',
    run_dir: '/tmp/desk-paper', kill_files: ['HALT'], owner: null,
    book: { 'canary-2026-09-28-a': { status: 'open', open_qty: 1 } },
    inbox: ['canary-2026-09-28-b.json'],
    last_results: [{
      schema: 'desk-entry-result/1', at: '2026-09-28T15:00:00+00:00',
      request: 'canary-2026-09-28-a.json', intent_id: 'canary-2026-09-28-a',
      status: 'refused', reason: 'kill_file_present',
    }],
    supervised: {
      schema: 'supervised-status/1', at: '2026-09-28T15:00:00+00:00',
      mandate: { state: 'active', days_left: 2, account_id: 'DU1234567',
        max_orders: 5, orders_used: 2, long_running: true,
        expires_at: '2026-10-01T14:30:00+00:00' },
      outbox: [],
    },
    events: [],
  })
  vi.mocked(getLabScoreboard).mockResolvedValue({
    schema: 'desk-lab-scoreboard/1', execution_enabled: false,
    policies: { 'model:zai': { runs: 3, boards: 12, model_calls: 12, model_failures: 0,
      entered: 4, modeled_wins: 2, modeled_losses: 2, closed_pnl_sum: '-5',
      worst_minimum_capital: '4970', last_run: 'run-c', kinds: ['model'] } },
    advisory: { policy: 'model:zai', promoted: false,
      basis: 'highest summed closed-pnl proxy over >= 3 runs',
      stats: { runs: 3, boards: 12, model_calls: 12, model_failures: 0,
        entered: 4, modeled_wins: 2, modeled_losses: 2, closed_pnl_sum: '-5',
        worst_minimum_capital: '4970', last_run: 'run-c', kinds: ['model'] } },
  })
  render(<ActionModelPage />)
  await waitFor(() => expect(screen.getByText('Compare a version')).toBeTruthy())
  expect(screen.getByText(/execution authorized: no/)).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Supervised paper desk' })).toBeTruthy()
  expect(screen.getByText(/Mandate active · 2\/5 orders used · 2 days left · long-running grant/)).toBeTruthy()
  expect(screen.getByText(/Kill files: HALT/)).toBeTruthy()
  expect(screen.getByText('canary-2026-09-28-a.json')).toBeTruthy()
  expect(screen.getByText(/refused — kill_file_present/)).toBeTruthy()
  expect(screen.getByText(/model:zai: 3 runs · 4 entered · 2W\/2L · closed-pnl proxy \$-5/)).toBeTruthy()
  expect(screen.getByText(/Advisory only, never promoted/)).toBeTruthy()
  expect(screen.queryByText(/Governed entry: disabled/)).toBeNull()
  expect(screen.getByText(/1 modeled wins; too few trades for a rate/)).toBeTruthy()
  expect(screen.queryByText(/100\.0% modeled wins/)).toBeNull()
  expect(screen.getByRole('region', { name: 'Trading system graph' })).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Portfolio risk scenarios' })).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Intraday action graphs' })).toBeTruthy()
  expect(screen.getByText(/512 snapshots across 64 sessions/)).toBeTruthy()
  expect(screen.getByText(/1\/2 modeled entries admitted/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /N01.*Resolve inputs/ }))
  expect(screen.getByText(/Guards: input_hash_match/)).toBeTruthy()
  expect(screen.getByText(/No attempt, grant, permit, broker effect/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: /submit|approve|trade/i })).toBeNull()
})

it('renders the automation card and issues whitelisted controls', async () => {
  vi.mocked(getAutomation).mockResolvedValue({
    schema: 'desk-automation/1',
    kill_files: [],
    timers: [{
      key: 'desk-lab', what: 'hourly flash boards', timer: 'desk-lab.timer',
      service: 'desk-lab.service', enabled: true, active: true,
      next_elapse: 'Mon 2026-09-28 18:17:00 MDT', last_result: 'success',
      last_exit: '',
    }],
  })
  vi.mocked(postAutomationAction).mockResolvedValue({
    key: 'desk-lab', action: 'disable', unit: 'desk-lab.timer' })
  vi.mocked(postSupervisedControl).mockResolvedValue({ kill_files: ['HALT'] })
  render(<ActionModelPage />)
  expect(await screen.findByTestId('timer-desk-lab')).toBeTruthy()
  expect(screen.getByText(/enabled · active/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Disable' }))
  await waitFor(() => expect(postAutomationAction).toHaveBeenCalledWith('desk-lab', 'disable'))
  fireEvent.click(screen.getByRole('button', { name: 'HALT' }))
  await waitFor(() => expect(postSupervisedControl).toHaveBeenCalledWith('halt'))
})

const pair = (diff: number, lo: number, hi: number) => ({ diff_total: diff, ci95: [lo, hi] as [number, number], p_one_sided: 0.25, sessions: 3 })

const LONG_RUN: LongRunView = {
  schema: 'desk-longrun-view/1', run: '20260929T000000Z', execution_enabled: false,
  progress: {
    schema: 'desk-longrun-progress/1', status: 'finished', at: '2026-09-29T01:00:00+00:00',
    started: '2026-09-29T00:00:00+00:00', boards: 6, sessions: 3, total: 54, finished: 54,
    failures: 2, paused_s: 600, quota: { ok: true, reason: 'left=90.0 planned=60.0', checked_at: null },
    calls_per_s: 1.2, eta_s: 0, digest: 'digest.json',
    arms: {
      'm31#1': { policy: 'm31', repeat: 1, kind: 'model', done: 6, total: 6, entered: 5, failures: 2, unevaluable: 1, net: 42.5 },
      no_trade: { policy: 'no_trade', repeat: 1, kind: 'control', done: 6, total: 6, entered: 0, failures: 0, unevaluable: 0, net: 0 },
    },
  },
  digest: {
    headline: 'A/A valid (choice agreement 83.3%); 2 walk-forward finalist(s) tested once; 0 eligible for operator review; nothing promoted.',
    untrusted_note: 'UNTRUSTED / NEVER PROMOTED. Model choices and notes are untrusted prose.',
    evaluation_valid: true, complete: true, at: '2026-09-29T01:00:00+00:00',
    promotion: { promoted: false, rule: 'Pre-registered in plan.json before any scoring.' },
    boards: { total: 6, scored: 4, excluded: 2, sessions: { count: 3, first: '2026-06-01', last: '2026-06-03' } },
    aa: { status: 'valid', valid: true, rule: 'INVALID when the CI excludes 0', pair: ['m31#1', 'm31#2'], boards: 4, agreement: 0.8333, diff: pair(-4, -20, 12) },
    random_null: { p_enter: 0.75, matched_to: ['m31#1', 'm31#2'], horizons: [null], seeds: 1000, expected_total: 7, expected_ci95: [3, 11], simulated_mean_total: 7.1, band95: [-30, 44] },
    standings: [{
      arm: 'm31#1', policy: 'm31', repeat: 1, kind: 'model', boards: 4, entered: 3, entry_rate: 0.75,
      unevaluable: 1, failures: 2, net_total: 42.5, net_ci95: [-10.25, 80],
      vs_random: pair(35.5, -17, 73), vs_first_row: pair(56, 10, 90), vs_incumbent: null,
      vs_regime: pair(-12, -60, 30), null_percentile: 0.91,
    }],
    walk_forward: { status: 'ok', cutoff: '2026-06-02', metric: 'ci_low_diff_vs_random', max_finalists: 2, tune_sessions: 2, test_sessions: 1, reason: null,
      finalists: [{ policy: 'm31', holm_p: 0.5, eligible_for_operator_review: false,
        test: { net_total: 12, net_ci95: [12, 12], vs_random: pair(9, 9, 9), vs_incumbent: null } }] },
    benchmarks: [{ name: 'SPY', status: 'ok', net_total: 118.2, net_ci95: [-40, 260], base_date: '2026-05-29', missing_sessions: 0 },
      { name: 'EW', status: 'unavailable', reason: 'no close' }],
  },
}

it('renders the long-run card: progress bars, CI standings, A/A, random band, benchmarks, never promoted', async () => {
  vi.mocked(getLongRun).mockResolvedValue(LONG_RUN)
  render(<ActionModelPage />)
  const card = await screen.findByRole('region', { name: 'Desk long run' })
  expect(await screen.findByTestId('longrun-arm-m31#1')).toBeTruthy()
  const bar = screen.getByRole('progressbar', { name: 'm31#1 progress' }) as HTMLProgressElement
  expect(bar.value).toBe(6)
  expect(bar.max).toBe(6)
  expect(screen.getByTestId('longrun-arm-m31#1').textContent).toMatch(/5 entered · 2 failed · running isolated net \+\$42\.50/)
  expect(card.textContent).toMatch(/54\/54 decisions · 2 failed · paused 10 min/)
  const row = screen.getByTestId('longrun-standing-m31#1')
  expect(row.textContent).toMatch(/\+\$42\.50 \[−\$10\.25, \+\$80\.00\]/)
  expect(row.textContent).toMatch(/1 unevaluable outcomes/)
  expect(row.textContent).toMatch(/−\$12\.00 \[−\$60\.00, \+\$30\.00\]$/) // vs the bullish regime
  expect(screen.getByTestId('longrun-aa').textContent).toMatch(/A\/A valid: m31#1 vs m31#2 agree on 83\.3% of 4 boards/)
  expect(screen.getByTestId('longrun-random-band').textContent).toMatch(/75\.0%, 1000 seeds.*95% null band \[−\$30\.00, \+\$44\.00\]/)
  expect(screen.getByTestId('longrun-random').textContent).toMatch(/\+\$7\.00 \[\+\$3\.00, \+\$11\.00\]/)
  expect(screen.getByTestId('longrun-benchmark-SPY').textContent).toMatch(/SPY buy-and-hold.*\+\$118\.20 \[−\$40\.00, \+\$260\.00\]/)
  expect(screen.queryByTestId('longrun-benchmark-EW')).toBeNull()
  expect(card.textContent).toMatch(/UNTRUSTED \/ NEVER PROMOTED/)
  expect(card.textContent).toMatch(/Never promoted: the pre-registered rule is text for the operator/)
  expect(card.textContent).toMatch(/at most 2 finalists tested once/)
  expect(card.textContent).not.toMatch(/eligible for operator review —/)
  expect(screen.queryByRole('button', { name: /promote/i })).toBeNull()
})

it('distinguishes own-entry-rate evidence from the legacy shared null', async () => {
  const own = structuredClone(LONG_RUN)
  Object.assign(own.digest!.standings[0], { vs_random_own: { ...pair(6, 1, 9), p_enter: 0.5 } })
  vi.mocked(getLongRun).mockResolvedValue(own)
  render(<ActionModelPage />)
  const card = await screen.findByRole('region', { name: 'Desk long run' })
  expect(screen.getByRole('columnheader', { name: 'vs random (own entry rate)' })).toBeTruthy()
  expect(screen.getByRole('columnheader', { name: 'vs random (legacy incumbent)' })).toBeTruthy()
  const row = screen.getByTestId('longrun-standing-m31#1')
  expect(row.textContent).toContain('+$6.00 [+$1.00, +$9.00]')
  expect(row.textContent).toContain('50.0%')
  expect(row.textContent).toContain('+$35.50 [−$17.00, +$73.00]')
  expect(card.textContent).toContain('Legacy incumbent comparison is descriptive')
})

it('marks historical scoring rules and does not reuse their review eligibility', async () => {
  const historical = structuredClone(LONG_RUN)
  historical.digest!.walk_forward.finalists[0].eligible_for_operator_review = true
  vi.mocked(getLongRun).mockResolvedValue(historical)
  render(<ActionModelPage />)
  const card = await screen.findByRole('region', { name: 'Desk long run' })
  expect(card.textContent).toContain('Historical scoring contract')
  expect(card.textContent).not.toContain('— eligible for operator review')
  expect(screen.queryByRole('button', { name: /promote/i })).toBeNull()
})

function registeredLongRun(): LongRunView {
  const current = structuredClone(LONG_RUN)
  Object.assign(current.digest!, { assessment_class: 'registered_protocol' })
  Object.assign(current.digest!.walk_forward, {
    scoring_version: 'split-local-own-rate/v2', null_scope: 'split_local_own_entry_rate',
    min_test_entries: 30, entry_count_unit: 'distinct_evaluated_decision_boards',
    assessment_class: 'registered_protocol',
  })
  Object.assign(current.digest!.walk_forward.finalists[0], {
    test_entries: 30, eligible_for_operator_review: true,
    rule_check: { test_net_positive: true, test_entries_at_least_floor: true,
      split_local_null_known: true, confirmatory_assessment: true,
      aa_valid: true, holm_p_below_alpha: true, vs_random_ci_low_above_0: true,
      vs_incumbent_ci_low_above_0: true, half_split_signs_agree: true,
      no_drop_one_sign_flip: true },
  })
  return current
}

it('shows preregistered distinct-entry floors with registered review evidence', async () => {
  vi.mocked(getLongRun).mockResolvedValue(registeredLongRun())
  render(<ActionModelPage />)
  const card = await screen.findByRole('region', { name: 'Desk long run' })
  expect(card.textContent).toContain('30 distinct evaluated decision boards')
  expect(card.textContent).toContain('30/30 evaluated entries')
  expect(card.textContent).toContain('— eligible for operator review')
  expect(screen.queryByRole('button', { name: /promote/i })).toBeNull()
})

it.each([{ entries: 29, net: 12 }, { entries: 30, net: 0 }])(
  'refuses contradictory eligibility with $entries entries and net $net', async ({ entries, net }) => {
    const current = registeredLongRun()
    Object.assign(current.digest!.walk_forward.finalists[0], { test_entries: entries })
    current.digest!.walk_forward.finalists[0].test.net_total = net
    vi.mocked(getLongRun).mockResolvedValue(current)
    render(<ActionModelPage />)
    const card = await screen.findByRole('region', { name: 'Desk long run' })
    expect(card.textContent).not.toContain('— eligible for operator review')
    expect(card.textContent).toContain('review conditions not met')
  },
)

it('distinguishes missing derived cost inputs from zero profit and refuses review', async () => {
  const current = registeredLongRun()
  Object.assign(current.digest!, {
    pricing_status: 'DATA_GATED', cost_model: 'derived-spread/1',
    no_price: { total: 3, by_arm: { m31: 3 }, by_reason: { no_delta: 3 } },
  })
  vi.mocked(getLongRun).mockResolvedValue(current)
  render(<ActionModelPage />)
  const card = await screen.findByRole('region', { name: 'Desk long run' })
  expect(card.textContent).toContain('NO_PRICE: 3 selected outcomes')
  expect(card.textContent).toContain('no_delta: 3')
  expect(card.textContent).toContain('Derived EOD cost sensitivity')
  expect(card.textContent).toContain('missing pricing is not zero profit')
  expect(card.textContent).not.toContain('— eligible for operator review')
})

it.each(['string-false', 'failed-holm', 'missing-stability'])(
  'refuses incomplete or contradictory persisted review evidence: %s', async (mode) => {
    const current = registeredLongRun()
    const finalist = current.digest!.walk_forward.finalists[0]
    if (mode === 'string-false') Object.assign(current.digest!, { evaluation_valid: 'false' })
    if (mode === 'failed-holm') finalist.rule_check!.holm_p_below_alpha = false
    if (mode === 'missing-stability') delete finalist.rule_check!.half_split_signs_agree
    vi.mocked(getLongRun).mockResolvedValue(current)
    render(<ActionModelPage />)
    const card = await screen.findByRole('region', { name: 'Desk long run' })
    expect(card.textContent).not.toContain('— eligible for operator review')
    expect(card.textContent).toContain('review conditions not met')
  },
)

it('flags an invalid A/A pair and the empty store', async () => {
  const invalid: LongRunView = { ...LONG_RUN, digest: { ...LONG_RUN.digest!, evaluation_valid: false,
    headline: 'EVALUATION INVALID - the A/A pair differs significantly; nothing promoted.',
    aa: { ...LONG_RUN.digest!.aa, status: 'INVALID', valid: false } } }
  vi.mocked(getLongRun).mockResolvedValue(invalid)
  render(<ActionModelPage />)
  expect(await screen.findByText(/EVALUATION INVALID/)).toBeTruthy()
  expect(screen.getByTestId('longrun-aa').textContent).toMatch(/A\/A INVALID/)
  cleanup()
  vi.mocked(getLongRun).mockResolvedValue({ schema: 'desk-longrun-view/1', run: null, progress: null, digest: null, execution_enabled: false })
  render(<ActionModelPage />)
  expect(await screen.findByText('No long run has started in this cockpit store.')).toBeTruthy()
})

// The account is not the desk book, and the canary's flat-book rule
// covers the CANDIDATE'S OWN UNDERLYING (Ruling 3b), not the whole
// account: a SPY candidate is admitted into an account holding NVDA legs.
const NON_FLAT_ACCOUNT: AccountExposure = {
  schema: 'desk-account-exposure/1', as_of: '2026-09-30',
  state_root: '/home/x/.local/state/trex', desk_run_dir: '/home/x/.local/state/trex/desk-paper',
  countable: true, max_loss_usd: '477.00', problems: [],
  desk_book: { structures: 0, legs: 0, max_loss_usd: '0.00', earliest_exit_deadline: null,
    exit_deadlines_unknown: 0, owners: [], books: [], positions: [] },
  outside_desk_book: { structures: 2, legs: 4, max_loss_usd: '477.00',
    earliest_exit_deadline: '2026-10-09', exit_deadlines_unknown: 0,
    owners: ['trex-monitor'], books: ['legacy:putspread-20260922'], positions: [] },
}

function _nonFlatMocks() {
  vi.mocked(getSupervisedDesk).mockResolvedValue({
    schema: 'desk-cli-status/1', at: '2026-09-30T13:00:00+00:00', run_dir: '/tmp/desk-paper',
    kill_files: [], owner: null, book: {}, inbox: [], last_results: [],
    account_exposure: NON_FLAT_ACCOUNT,
    supervised: { schema: 'supervised-status/1', at: '2026-09-30T13:00:00+00:00',
      mandate: { state: 'active', days_left: 1, account_id: 'DU1234567', orders_used: 0, max_orders: 3 },
      outbox: [] },
    events: [],
  })
  vi.mocked(getPlans).mockResolvedValue({
    now: '2026-09-30T13:00:00+00:00', gateway_reachable: true, plans: [],
    portfolio: {
      plans_count: 0, plans_with_state: 0, open_qty: 0, committed_at_caps: 0,
      committed_filled: 0, committed_known: 0, cost_unknown: false, unpriced_qty: 0,
      unrealized_open: 0, unrealized_filled: 0, short_floor: 0, long_ceiling: 0,
      legs: [],
    } as unknown as PlansResponse['portfolio'],
    account: null, accounts_seen: [],
    net_positions: [{
      underlying: 'NVDA', structure_count: 2, open_qty: 8, avg_entry: 0.64,
      committed: 477, committed_known: 477, cost_unknown: false, unpriced_qty: 0,
      max_gain: null, max_loss: 477, unrealized: null,
      short_floor: 150, long_ceiling: 185, legs: [],
    }],
  })
}

it('stands a red account banner over the desk book when the account is not flat', async () => {
  _nonFlatMocks()
  const { container } = render(<ActionModelPage />)
  const banner = await screen.findByTestId('account-exposure-banner')
  expect(banner.getAttribute('role')).toBe('alert')
  expect(banner.textContent).toMatch(/OUTSIDE the desk book/)
  expect(banner.textContent).toMatch(/4 legs \/ 2 structures/)
  expect(banner.textContent).toMatch(/trex-monitor/)
  expect(banner.textContent).toMatch(/earliest exit 2026-10-09/)
  // standing: no dismiss control, and it sits before the desk book lines
  expect(banner.querySelector('button')).toBeNull()
  const html = container.innerHTML
  expect(html.indexOf('account-exposure-banner')).toBeLessThan(html.indexOf('Kill files: none'))
})

it('does not claim the canary is blocked by the whole legacy book', async () => {
  _nonFlatMocks()
  render(<ActionModelPage />)
  await screen.findByTestId('account-exposure-banner')
  const text = document.body.textContent ?? ''
  expect(text).not.toMatch(/remains blocked until the legacy book is flat/)
  expect(text).toMatch(/only on the candidate’s own underlying \(Ruling 3b\)/)
  expect(text).toMatch(/admitted into this non-flat account/)
  // the account's real shape, not one underlying row
  expect(text).toMatch(/2 structures, 4 option legs, \$477\.00 max loss/)
  expect(text).toMatch(/underlying rows/)
})
