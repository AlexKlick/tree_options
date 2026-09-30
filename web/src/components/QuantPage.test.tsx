import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { getQuantLab } from '../lib/api'
import { QuantPage } from './QuantPage'

vi.mock('../lib/api', () => ({getQuantLab: vi.fn()}))
afterEach(() => {cleanup(); vi.clearAllMocks()})

it('keeps broker filled distinct from exact economics and distinguishes evidence lanes', async () => {
  vi.mocked(getQuantLab).mockResolvedValue({
    strategies: [{strategy_id: 'robust_value_5metric', version: '1', registration: 'reference_baseline', data_status: 'requires_pit_ratio_reconstruction', description: 'Historical PIT ratios required', required_inputs: ['filing_date']}],
    versions: [], experiments: [], comparisons: [],
    evidence_classes: ['BACKTEST', 'DETERMINISTIC REPLAY', 'SIMULATED EXECUTION', 'BROKER PAPER', 'LIVE'],
    execution: {state: 'STALE', environment: 'BROKER PAPER', account_alias: 'paper-test', executions: [{intent_id: 'one', state: 'FILLED', broker_state: 'FILLED', reconciliation_clean: false, findings: ['FILL_ECONOMIC_GAP'], evidence_verdict: 'REFUSED', exact_economics: false, records: []}]},
    live_money: false, execution_authorized: false,
  })
  render(<QuantPage />)
  await waitFor(() => expect(screen.getByText('Historical PIT ratios required')).toBeTruthy())
  expect(screen.getByText('reference_baseline · DATA GATED')).toBeTruthy()
  expect(screen.getByText('Broker order state: FILLED')).toBeTruthy()
  expect(screen.getByText('Exact fill economics: unavailable · Evidence: REFUSED')).toBeTruthy()
  expect(screen.getByText(/DETERMINISTIC REPLAY · SIMULATED EXECUTION · BROKER PAPER/)).toBeTruthy()
  expect(screen.queryByRole('button', {name: /submit|approve/i})).toBeNull()
})
