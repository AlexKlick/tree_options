import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ResearchPage } from './ResearchPage'

// Workspace-flow mocks: catalog -> run -> recorded result. The result
// payload mirrors the backend wire contract exactly (to_wire): ISO date
// keys, money strings — the page must render FROM THE SAME RESULT the
// chart/table/tiles would consume.
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
    warnings: ['research.synthetic_v1_machinery_validation'],
    source_url: 'synthetic/synthetic-benchmark-v1',
  },
  {
    id: 'synthetic-momentum-v1',
    family: 'synthetic-momentum-v1',
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
    warnings: ['research.synthetic_v1_machinery_validation'],
    source_url: 'synthetic/synthetic-momentum-v1',
  },
  {
    id: 'term-gate-v?',
    family: 'term-gate',
    version: 'v?',
    evidence_kind: 'sealed_campaign',
    registration: 'retrospective_backfill',
    disposition: 'WITHDRAWN',
    plot_funded_account: false,
    supported_start: null,
    supported_end: null,
    funded_history: 'unavailable',
    funded_history_reason: 'no daily portfolio history is reconstructable from sealed trials',
    artifact_hashes: {},
    capabilities: ['view_published_study'],
    ineligibility_reason: 'disposition=WITHDRAWN',
    data_completeness: {},
    warnings: ['research.data_gated'],
    source_url: 'sealed-round/term-gate',
  },
]

const ROWS: Record<string, {
  cash: string; inventory: [string, number][]; marked_value: string | null
  nav: string | null; contributions_cum: string; withdrawals_cum: string
  fees_cum: string; realized_pnl_cum: string; investment_gain: string | null
  missing_mark_symbols: string[]
}> = {
  '2024-01-02': {
    cash: '995.20', inventory: [['SPY', 24]], marked_value: '9600.00', nav: '10595.20',
    contributions_cum: '0.00', withdrawals_cum: '0.00', fees_cum: '4.80',
    realized_pnl_cum: '0.00', investment_gain: '0.00', missing_mark_symbols: [],
  },
  '2024-02-15': {
    cash: '995.20', inventory: [['SPY', 24]], marked_value: '9792.00', nav: '10787.20',
    contributions_cum: '0.00', withdrawals_cum: '0.00', fees_cum: '4.80',
    realized_pnl_cum: '0.00', investment_gain: '192.00', missing_mark_symbols: [],
  },
  '2024-03-28': {
    cash: '995.20', inventory: [['SPY', 24]], marked_value: '9888.00', nav: '10883.20',
    contributions_cum: '0.00', withdrawals_cum: '0.00', fees_cum: '4.80',
    realized_pnl_cum: '0.00', investment_gain: '288.00', missing_mark_symbols: [],
  },
}

const RESULT_ENVELOPE = {
  run_id: 'r1',
  status: 'completed' as const,
  result: {
    spec: {
      candidate_ids: ['synthetic-benchmark-v1', 'synthetic-momentum-v1'],
      starting_capital: '10000',
      common_start: '2024-01-02',
      common_end: '2024-03-28',
      cashflow_timing: 'beginning_of_period',
      contribution_per_period: '0',
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
    candidates: [
      {
        candidate_id: 'synthetic-benchmark-v1',
        candidate: CANDIDATES[0],
        rows_by_date: ROWS,
        drawdown: {},
        fees_paid_total: '4.80',
        excluded_out_of_window: 0,
        sample_size: 3,
        sample_floor: 20,
        sample_floor_met: false,
        rejection_reason: null,
        final_ending_value: '10883.20',
      },
      {
        candidate_id: 'term-gate-v?',
        candidate: CANDIDATES[2],
        rows_by_date: {},
        drawdown: {},
        fees_paid_total: '0.00',
        excluded_out_of_window: 0,
        sample_size: 0,
        sample_floor: 20,
        sample_floor_met: false,
        rejection_reason: 'no funded history (WITHDRAWN): disposition=WITHDRAWN',
        final_ending_value: null,
      },
    ],
    paired_diff: {},
  },
  result_sha256: 'x64',
  engine_sha256: 'x64',
  input_snapshot_sha256: 'x64',
  calendar_sha256: 'x64',
}

const mocks = vi.hoisted(() => ({
  resultCalls: 0,
}))

vi.mock('../lib/api', () => ({
  listResearchCandidates: async () => ({ candidates: CANDIDATES }),
  getResearchEvidence: async () => ({
    candidate_id: 'term-gate-v?',
    point_session: null,
    hypothesis: 'Sealed-round verdict',
    estimand: 'campaign disposition',
    exact_versions: {},
    cohort_membership: [],
    registered_or_exploratory: 'retrospective_backfill',
    diagnostics: {},
    robustness: [],
    source_artifacts: [],
    reproduction_command: 'python -m tree_options.research inspect --candidate term-gate-v?',
    warnings: [],
  }),
  getResearchRun: async () => ({ run_id: 'r1', status: 'completed', spec_hash: 'r1', workspace: '/tmp' }),
  getComparisonResult: async () => {
    mocks.resultCalls += 1
    return RESULT_ENVELOPE
  },
  spoolComparison: async () => ({ run_id: 'r1', status: 'queued', spec_hash: 'r1', workspace: '/tmp' }),
}))

describe('ResearchPage (RL-1 completion workspace)', () => {
  it('lists candidates with data capability and blockers separated', async () => {
    render(<ResearchPage />)
    await waitFor(() => {
      expect(screen.getByText('term-gate')).toBeTruthy()
    })
    expect(screen.getByText('WITHDRAWN')).toBeTruthy()
    expect(screen.getByText('unavailable')).toBeTruthy() // funded_history column
    expect(screen.getAllByText('SYNTHETIC').length).toBeGreaterThan(0)
  })

  it('runs the comparison and renders the recorded result end-to-end', async () => {
    const { container } = render(<ResearchPage />)
    // catalog loads; two synthetic candidates are auto-picked
    await waitFor(() => {
      expect(screen.getAllByRole('checkbox').length).toBe(2)
    })
    // window dates required by the spec
    const dateInputs = container.querySelectorAll('input[type="date"]')
    fireEvent.change(dateInputs[0], { target: { value: '2024-01-02' } })
    fireEvent.change(dateInputs[1], { target: { value: '2024-03-28' } })
    fireEvent.click(screen.getByText('Run comparison'))

    // the recorded result renders: tiles, values from the SAME payload,
    // the table's dated rows, and the rejected candidate's honest blocker
    await waitFor(() => {
      // the value appears in the summary tile, chart end-label, and table
      expect(screen.getAllByText('$10,883.20').length).toBeGreaterThan(0)
    })
    expect(screen.getByText(/coverage 3\/3 sessions/)).toBeTruthy()
    expect(screen.getByText('2024-02-15')).toBeTruthy()
    expect(screen.getByText(/no funded history \(WITHDRAWN\)/)).toBeTruthy()
    // the result endpoint is read once for the run — GET never recomputes
    expect(mocks.resultCalls).toBe(1)
  })

  it('opens the evidence drawer as a one-shot per candidate+session', async () => {
    const { container } = render(<ResearchPage />)
    await waitFor(() => {
      expect(container.querySelectorAll('input[type="date"]').length).toBe(2)
    })
    const dateInputs = container.querySelectorAll('input[type="date"]')
    fireEvent.change(dateInputs[0], { target: { value: '2024-01-02' } })
    fireEvent.change(dateInputs[1], { target: { value: '2024-03-28' } })
    fireEvent.click(screen.getByText('Run comparison'))
    await waitFor(() => {
      expect(screen.getByText('Values by session')).toBeTruthy()
    })
    fireEvent.click(screen.getAllByText('inspect')[0])
    await waitFor(() => {
      expect(screen.getByText(/python -m tree_options.research inspect/)).toBeTruthy()
    })
  })
})
