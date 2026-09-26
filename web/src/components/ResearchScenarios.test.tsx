import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ResearchScenarios } from './ResearchScenarios'

const mocks = vi.hoisted(() => ({
  listCalls: [] as string[],
  scenarioRuns: [] as Array<{
    child_run_id: string
    parent_run_id: string
    scenario_kind: string
    scenario_diff_sha256: string
  }>,
  lastResultEnvelope: null as null | object,
  resultEnvelope: null as null | object,
}))

const CANDIDATES = [
  {
    id: 'synthetic-benchmark-v1',
    family: 'synthetic-benchmark-v1',
    version: 'v1',
    evidence_kind: 'synthetic_backtest',
    registration: 'before_entry_window_end',
    disposition: 'PASS',
    plot_funded_account: true,
    supported_start: '2024-01-02',
    supported_end: '2024-03-28',
    funded_history: 'reconstructed',
    funded_history_reason: null,
    artifact_hashes: {},
    capabilities: ['plot_funded_account'],
    ineligibility_reason: null,
    data_completeness: {},
    warnings: [],
    source_url: 'synthetic/synthetic-benchmark-v1',
  },
]

const RESULT_ENVELOPE = {
  run_id: 'r1',
  status: 'completed',
  result: {
    spec: {
      candidate_ids: ['synthetic-benchmark-v1'],
      starting_capital: '10000',
      common_start: '2024-01-02',
      common_end: '2024-03-28',
      cashflow_timing: 'beginning_of_period',
      contribution_per_period: '500',
      cost_model_kind: 'five_bp_fixed',
      benchmark_candidate_id: 'synthetic-benchmark-v1',
      currency: 'USD',
      price_basis: 'nominal_pretax',
      idle_cash_policy: 'cash_yields_zero',
      rebalancing: 'none',
      position_sizing: 'integer',
      collateral: 'none',
      borrowing: 'none',
      knowledge_cutoff: null,
      proposed_by: 'operator',
      notes: '',
    },
    rejection: null,
    candidates: [{
      candidate_id: 'synthetic-benchmark-v1',
      candidate: CANDIDATES[0],
      rows_by_date: {
        '2024-01-02': {
          cash: '995.20', inventory: [['SPY', 24]],
          marked_value: '9600.00', nav: '10595.20',
          contributions_cum: '500.00', withdrawals_cum: '0.00',
          fees_cum: '4.80', realized_pnl_cum: '0.00',
          investment_gain: '95.20', missing_mark_symbols: [],
        },
        '2024-03-28': {
          cash: '995.20', inventory: [['SPY', 24]],
          marked_value: '9888.00', nav: '10883.20',
          contributions_cum: '1500.00', withdrawals_cum: '0.00',
          fees_cum: '4.80', realized_pnl_cum: '0.00',
          investment_gain: '383.20', missing_mark_symbols: [],
        },
      },
      drawdown: {},
      fees_paid_total: '4.80',
      excluded_out_of_window: 0,
      sample_size: 2,
      sample_floor: 20,
      sample_floor_met: false,
      rejection_reason: null,
      final_ending_value: '10883.20',
    }],
    paired_diff: {},
  },
  result_sha256: 'r64',
  engine_sha256: 'e64',
  input_snapshot_sha256: 'i64',
  calendar_sha256: 'c64',
}

vi.mock('../lib/api', () => ({
  listResearchCandidates: async () => ({ candidates: CANDIDATES }),
  listScenarios: async (parent_run_id?: string) => {
    mocks.listCalls.push(parent_run_id ?? '(none)')
    return { scenarios: mocks.scenarioRuns, parent_run_id: parent_run_id ?? null }
  },
  spoolScenario: async () => {
    mocks.lastResultEnvelope = RESULT_ENVELOPE
    const child_run_id = 'r1'
    mocks.scenarioRuns = [{
      child_run_id,
      parent_run_id: 'synthetic-benchmark-v1',
      scenario_kind: 'contribution_planning',
      scenario_diff_sha256: 'x64',
    }]
    return {
      run_id: child_run_id,
      status: 'completed',
      spec_hash: child_run_id,
      parent_run_id: 'synthetic-benchmark-v1',
      workspace: '/tmp',
    }
  },
  getComparisonResult: async () => mocks.resultEnvelope ?? RESULT_ENVELOPE,
  getResearchRun: async () => ({ run_id: 'r1', status: 'completed', spec_hash: 'r1', workspace: '/tmp' }),
}))

describe('ResearchScenarios (RL-2)', () => {
  it('renders the scenarios workspace with parent + diff form', async () => {
    render(<ResearchScenarios />)
    await waitFor(() => {
      expect(screen.getByTestId('scenarios-parent-input')).toBeTruthy()
    })
    expect(screen.getByTestId('scenarios-kind-select')).toBeTruthy()
    expect(screen.getByTestId('scenarios-contribution-input')).toBeTruthy()
    expect(screen.getByTestId('scenarios-access-select')).toBeTruthy()
  })

  it('submits a contribution fork and renders the child totals from the recorded result', { timeout: 15_000 }, async () => {
    render(<ResearchScenarios />)
    await waitFor(() => {
      expect(screen.getByTestId('scenarios-parent-input')).toBeTruthy()
    })
    // The parent field starts EMPTY by design (P2-5: seeding it from
    // a candidate id guaranteed a 404); the operator pastes a
    // completed run's id.
    fireEvent.change(screen.getByTestId('scenarios-parent-input'), {
      target: { value: 'a'.repeat(64) },
    })
    const contribution = screen.getByTestId('scenarios-contribution-input')
    fireEvent.change(contribution, { target: { value: '500' } })
    fireEvent.click(screen.getByTestId('scenarios-submit'))
    // After submit, pickedChild flips; the totals row carries the
    // FINAL NAV exactly as the worker published it (parity oracle).
    await waitFor(() => {
      const row = document.querySelector(
        '[data-testid="scenarios-total-synthetic-benchmark-v1"]',
      )
      expect(row).toBeTruthy()
    }, { timeout: 8_000 })
    expect(screen.getAllByText('$10,883.20').length).toBeGreaterThan(0)
  })

  it('renders a refused scenario as the honest blocker, never an empty chart', { timeout: 15_000 }, async () => {
    // P2-3 oracle: the refusal wire ({refusal, message}) carries no
    // candidates, so gating on status alone used to render a blank
    // workspace. The blocker must name the machine-readable code.
    mocks.resultEnvelope = {
      run_id: 'r2',
      status: 'completed',
      result: {
        refusal: 'research.scenario.stress_unsupported',
        message: 'RL-2 ships no option-valuation shock engine',
        scenario_kind: 'conditional_stress',
      },
    }
    render(<ResearchScenarios />)
    await waitFor(() => {
      expect(screen.getByTestId('scenarios-parent-input')).toBeTruthy()
    })
    fireEvent.change(screen.getByTestId('scenarios-parent-input'), {
      target: { value: 'b'.repeat(64) },
    })
    fireEvent.click(screen.getByTestId('scenarios-submit'))
    await waitFor(() => {
      expect(screen.getByTestId('scenarios-refusal')).toBeTruthy()
    }, { timeout: 8_000 })
    expect(screen.getByText(/research\.scenario\.stress_unsupported/)).toBeTruthy()
    expect(screen.queryByTestId('scenarios-totals')).toBeNull()
  })
})
