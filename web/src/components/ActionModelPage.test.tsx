import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ActionModelPage } from './ActionModelPage'
import { getActionModelExample, getAutomation, getDeskStandings, getHistoricalReplays, getIntradayGraphs, getLabScoreboard, getLongRun, getPlans, getPortfolioScenarios, getSupervisedDesk, postAutomationAction, postSupervisedControl } from '../lib/api'
import type { AccountExposure, DeskStandings, LongRunView, PlansResponse } from '../lib/types'

vi.mock('../lib/api', () => ({
  getActionModelExample: vi.fn(),
  getPlans: vi.fn(),
  getHistoricalReplays: vi.fn(),
  getIntradayGraphs: vi.fn(),
  getPortfolioScenarios: vi.fn(),
  getSupervisedDesk: vi.fn(),
  getLabScoreboard: vi.fn(),
  getLongRun: vi.fn(),
  getDeskStandings: vi.fn(),
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
    standings: [
      // Server order per #45's sort (own-null ci95 low desc: no_trade 0 > m31#1
      // −18). no_trade identity pinned by #45's test_vs_random_is_matched_to_each_
      // arms_own_entry_rate: p_enter 0.0, diff_total == net_total == 0.0. Its
      // legacy vs_random is hand-derived: 0.00 − 7.00 (the shared null's
      // expected_total at the pooled 75% rate, from random_null below).
      { arm: 'no_trade', policy: 'no_trade', repeat: 1, kind: 'control', boards: 4, entered: 0,
        entry_rate: 0.0, unevaluable: 0, failures: 0, net_total: 0, net_ci95: [0, 0],
        vs_random: pair(-7, -44, 30), vs_first_row: null, vs_incumbent: null, vs_regime: null,
        null_percentile: 0.5, vs_random_own: { ...pair(0, 0, 0), p_enter: 0.0 },
        null_percentile_own: 0.5 },
      // vs_random_own: own-null expected_total 10.50 at this arm's own 75% rate
      // (fixture input), so diff_total = 42.50 − 10.50 = 32.00 (paired shape +
      // p_enter per #45's keep list).
      { arm: 'm31#1', policy: 'm31', repeat: 1, kind: 'model', boards: 4, entered: 3, entry_rate: 0.75,
        unevaluable: 1, failures: 2, net_total: 42.5, net_ci95: [-10.25, 80],
        vs_random: pair(35.5, -17, 73), vs_first_row: pair(56, 10, 90), vs_incumbent: null,
        vs_regime: pair(-12, -60, 30), null_percentile: 0.91,
        vs_random_own: { ...pair(32, -18, 72), p_enter: 0.75 }, null_percentile_own: 0.92 },
    ],
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
  expect(row.textContent).toMatch(/1 no fill/)
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
  expect(screen.queryByTestId('longrun-skill')).toBeNull()
  expect(screen.queryByTestId('longrun-skill-no-price')).toBeNull()
  expect(screen.queryByRole('button', { name: /promote/i })).toBeNull()
})

it('renders vs own-null as the standings headline (p_enter stated) and labels the legacy shared-null column', async () => {
  vi.mocked(getLongRun).mockResolvedValue(LONG_RUN)
  render(<ActionModelPage />)
  await screen.findByTestId('longrun-standing-m31#1')
  const legacyHead = screen.getByRole('columnheader', { name: /vs random \(legacy\)/ })
  expect(screen.getByRole('columnheader', { name: /vs random \(own entry rate\)/ })).toBeTruthy()
  // the legacy column must be unreadable as a skill measure
  expect(legacyHead.getAttribute('title')).toMatch(/not a skill measure/i)
  expect(legacyHead.getAttribute('title')).toMatch(/single shared constant/i)
  expect(screen.getByTestId('longrun-standing-m31#1').textContent)
    .toMatch(/\+\$32\.00 \[−\$18\.00, \+\$72\.00\] · p_enter 75\.0%/)
  expect(screen.getByTestId('longrun-standing-no_trade').textContent)
    .toMatch(/\+\$0\.00 \[\+\$0\.00, \+\$0\.00\] · p_enter 0\.0%/)
})

it('keeps the server\'s standings order — no client sort on the legacy column or net', async () => {
  vi.mocked(getLongRun).mockResolvedValue(LONG_RUN)
  render(<ActionModelPage />)
  await screen.findByTestId('longrun-standing-m31#1')
  const order = screen.getAllByTestId(/^longrun-standing-/).map((el) => el.getAttribute('data-testid'))
  // fixture order [no_trade, m31#1] is what the #45 server sort produces
  // (own-null ci95 low desc: 0 > −18). A client sort on the legacy column
  // (ci95 low −44 < −17) or on net (0 < 42.5) would flip this and fail here.
  expect(order).toEqual(['longrun-standing-no_trade', 'longrun-standing-m31#1'])
})

it('renders the promotion outcome from the server with the evaluated-entries floor; a net_total=0 abstention is NOT eligible', async () => {
  // Real case from PR #47's body (run 20260929T094303Z): finalist
  // refl-trend-persistent-bullish-trend scored a test-window net of exactly
  // $0.00 yet a vs_random CI of [+540.97, +2398.67] — it won the comparison
  // by abstaining. #47's rule adds test_net_positive and
  // test_entries_at_least_floor (MIN_TEST_ENTRIES = 30); the server folds
  // those into eligible_for_operator_review, and the SPA renders that verdict
  // plus the counts (15 = at most 15 evaluated fills exist run-wide). It
  // deliberately mirrors NO clause names — that is how the old local
  // ActionModelPage mirror drifted in the first place.
  const view: LongRunView = { ...LONG_RUN, digest: { ...LONG_RUN.digest!,
    walk_forward: { ...LONG_RUN.digest!.walk_forward, min_test_entries: 30,
      finalists: [
        { policy: 'refl-trend-persistent-bullish-trend', holm_p: 0.004, test_entries: 15,
          eligible_for_operator_review: false,
          test: { net_total: 0, net_ci95: [0, 0],
            vs_random: { diff_total: 1494.53, ci95: [540.97, 2398.67], p_one_sided: 0.002, sessions: 29 },
            vs_incumbent: { diff_total: -703.5, ci95: [-3483.27, 1664.94], p_one_sided: 0.6906, sessions: 29 } } },
        { policy: 'm31', holm_p: 0.5, test_entries: 42, eligible_for_operator_review: true,
          test: { net_total: 12, net_ci95: [12, 12], vs_random: pair(9, 9, 9), vs_incumbent: null } },
      ] } } }
  vi.mocked(getLongRun).mockResolvedValue(view)
  render(<ActionModelPage />)
  const wf = await screen.findByTestId('longrun-wf')
  expect(wf.textContent).toMatch(/refl-trend-persistent-bullish-trend test \+\$0\.00.*15\/30 evaluated test entries/)
  expect(wf.textContent).toMatch(/m31 test.*Holm p 0\.500, 42\/30 evaluated test entries — eligible for operator review/)
  expect(wf.textContent).not.toMatch(/refl-trend[^;]*— eligible for operator review/)
})

it('renders the skill verdicts and the NO PRICE ledger — a fully-refused arm cannot read as break-even', async () => {
  // Served shape is PR #50's (skill.cockpit_projection): {no_price: <section
  // ledger>, arms: {name: {...}}}. Ledger numbers are #48's STRICT fixture
  // (tests/unit/test_desk_no_price_propagation.py): s3 unknown_symbol, s4
  // no_delta, s5 delta_out_of_universe -> 3 dropped, by_arm {m31#1: 3},
  // verdict prefix "NO PRICE (3 dropped): ". cost_provenance is #48's
  // CostProvenance.measured_corpus().as_dict() (desk/cost.py): EOD chain
  // snapshots, describes_fill_clock hard-coded false.
  const view: LongRunView = { ...LONG_RUN, digest: { ...LONG_RUN.digest!,
    skill: {
      no_price: { total: 3, by_arm: { 'm31#1': 3 },
        by_reason: { unknown_symbol: 1, no_delta: 1, delta_out_of_universe: 1 } },
      arms: {
        'm31#1': { verdict: 'NO PRICE (3 dropped): NO SKILL DETECTED (excess CI straddles 0) — Descriptive; nothing promoted.',
          excess_total: 0, excess_block_ci95: [-1, 1], forward_significant: false,
          no_price: { total: 3, snapshots: ['s3', 's4', 's5'],
            reasons: { s3: 'unknown_symbol', s4: 'no_delta', s5: 'delta_out_of_universe' } },
          boards_dropped_unpriced: 3,
          cost_provenance: { source: 'cboe-delayed-eod-chains', snapshot_window_et: '17:45-06:30',
            universe_filter: 'symbol in IWM/QQQ/SPY, 7 <= dte <= 60, volume > 0, oi > 0, |delta| <= 0.70',
            n_rows: 18783, decision_clocks_et: ['10:00', '10:15', '15:15'],
            describes_fill_clock: false,
            gap: 'the cboe-delayed-eod-chains corpus was captured 17:45-06:30 ET, outside every decision clock this desk trades (10:00, 10:15, 15:15 ET); no instant of the capture window describes a fill at a decision clock, and this gap is not falsifiable from the corpus' } },
        no_trade: { verdict: 'NO TRADES (entered 0) — Descriptive; nothing promoted.',
          excess_total: 0, excess_block_ci95: null, forward_significant: false,
          no_price: null, boards_dropped_unpriced: 0, cost_provenance: null },
      } } } }
  vi.mocked(getLongRun).mockResolvedValue(view)
  render(<ActionModelPage />)
  const skillLine = await screen.findByTestId('longrun-skill')
  expect(skillLine.textContent).toMatch(/NO PRICE \(3 dropped\)/)
  expect(skillLine.textContent).toMatch(/m31#1: NO PRICE \(3 dropped\).* — 3 boards dropped unpriced/)
  expect(skillLine.textContent).toMatch(/no_trade: NO TRADES/)
  const noPrice = screen.getByTestId('longrun-skill-no-price')
  expect(noPrice.textContent).toMatch(/3 boards dropped unpriced/)
  expect(noPrice.textContent).toMatch(/unknown_symbol 1, no_delta 1, delta_out_of_universe 1/)
  // cost-derived numbers never display without their cost basis: the source
  // and the fill-clock caveat ride along whenever provenance is present
  const cost = screen.getByTestId('longrun-skill-cost')
  expect(cost.textContent).toMatch(/cboe-delayed-eod-chains/)
  expect(cost.textContent).toMatch(/do not describe the fill clock/)
})

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

// Mirrors challenge.UNTRUSTED_NOTE verbatim (the footnote must quote it).
const UNTRUSTED_NOTE =
  "Model output is untrusted prose. Outcomes are mechanical proxies from " +
  "replay accounting on last-traded-minute closes, not executable fills. " +
  "Nothing in this digest is promoted: promotion is the operator's " +
  "pre-registered-rule path (docs/desk/PROMOTION-RULE.md)."

const STANDINGS: DeskStandings = {
  schema: 'desk-challenge-standings/1',
  registration_sample_through: '20261001T162152Z',
  games_counted: 2,
  cost_baseline_per_game: 14.6,
  policies: [
    // hand arithmetic over two fixture digests: winner 30+25 boards,
    // pnl 8+(-3); the control 30+25 boards, pnl 1+0
    { policy: 'gepa:alpha', kind: 'model', games: 2, boards: 55, entered: 9,
      closed_pnl_sum: '5', worst_minimum_capital: '4980', model_calls: 20,
      model_failures: 1, sessions_distinct: 2,
      session_pnl: { '20261002T010000Z:2026-10-02': 8.0, '20261003T010000Z:2026-10-03': -3.0 } },
    { policy: 'no_trade', kind: 'rules', games: 2, boards: 55, entered: 0,
      closed_pnl_sum: '1', worst_minimum_capital: '4999', model_calls: 0,
      model_failures: 0, sessions_distinct: 2, session_pnl: {} },
  ],
  untrusted_note: UNTRUSTED_NOTE,
}

it('renders the rule standings: per-policy rows, cost badge, the note verbatim', async () => {
  vi.mocked(getDeskStandings).mockResolvedValue(STANDINGS)
  render(<ActionModelPage />)
  const card = await screen.findByRole('region', { name: 'Desk challenge rule standings' })
  const winner = await screen.findByTestId('standings-row-gepa:alpha')
  expect(winner.textContent).toMatch(/gepa:alpha/)
  expect(winner.textContent).toMatch(/model/) // kind column
  expect(card.textContent).toMatch(/Games.*Boards.*Closed pnl.*Sessions/s)
  expect(winner.textContent).toMatch(/\+\$5\.00/) // closed_pnl_sum "5"
  const control = screen.getByTestId('standings-row-no_trade')
  expect(control.textContent).toMatch(/no_trade/)
  expect(control.textContent).toMatch(/\+\$1\.00/)
  expect(card.textContent).toMatch(/2 post-seal games/)
  expect(card.textContent).toMatch(/cost baseline \$14\.60 \/ game/)
  expect(card.textContent).toMatch(/registration sample through 20261001T162152Z/)
  // the footnote quotes the standings' untrusted note VERBATIM
  expect(screen.getByTestId('standings-untrusted-note').textContent).toBe(UNTRUSTED_NOTE)
  // read-only card: no promote control ever
  expect(screen.queryByRole('button', { name: /promote/i })).toBeNull()
})

it('answers an empty standings store honestly', async () => {
  vi.mocked(getDeskStandings).mockResolvedValue({ ...STANDINGS, games_counted: 0, policies: [] })
  render(<ActionModelPage />)
  expect(await screen.findByText('No post-seal challenge game has completed yet.')).toBeTruthy()
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
