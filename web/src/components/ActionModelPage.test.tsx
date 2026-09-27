import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ActionModelPage } from './ActionModelPage'
import { getActionModelExample, getHistoricalReplays, getIntradayGraphs, getPlans, getPortfolioScenarios } from '../lib/api'

vi.mock('../lib/api', () => ({
  getActionModelExample: vi.fn(),
  getPlans: vi.fn(),
  getHistoricalReplays: vi.fn(),
  getIntradayGraphs: vi.fn(),
  getPortfolioScenarios: vi.fn(),
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
  render(<ActionModelPage />)
  await waitFor(() => expect(screen.getByText('Compare a version')).toBeTruthy())
  expect(screen.getByText(/execution authorized: no/)).toBeTruthy()
  expect(screen.getByText(/Governed entry: disabled/)).toBeTruthy()
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
