import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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

function campaign(dataClass: 'synthetic_fixture' | 'user_supplied_unqualified') {
  const metrics = {
    evidence_kind: 'BACKTEST' as const,
    capital_policy: 'independent_equal_capital_roundtrips' as const,
    disposition: 'SCORED' as const,
    period_count: 3, scored_period_count: 3, compound_nav: null,
    max_drawdown_scope: 'endpoint_loss_only' as const,
    mean_net_return: '0.012', max_drawdown: '0.03', turnover: '1.98', fees: '10',
    execution_authorized: false as const, exact_external_economics: false as const,
  }
  return {
    schema: 'quant-theory-result/1' as const, campaign_id: 'a'.repeat(64),
    hypothesis: 'Compare strategies on frozen inputs', data_class: dataClass,
    evidence_kind: dataClass === 'synthetic_fixture' ? 'synthetic_backtest' as const : 'simulated_execution' as const,
    registration: 'exploratory_retrospective' as const, candidate_count: 2, reflection_calls: 0,
    winner: {strategy_id: 'momentum_12_1', parameters: {top_n: 2}, version_id: 'd'.repeat(64)},
    holdout: {candidate: metrics, control: {...metrics, mean_net_return: '0.001'}},
    graph: [
      {schema: 'quant-research-node/1' as const, campaign_id: 'a'.repeat(64), node_id: 'b'.repeat(64), stage: 'freeze_inputs', payload_sha256: 'c'.repeat(64), payload_ref: `runstate:quant_provenance/${'c'.repeat(64)}`, parents: [], execution_authorized: false as const},
      {schema: 'quant-research-node/1' as const, campaign_id: 'a'.repeat(64), node_id: 'e'.repeat(64), stage: 'sealed_holdout', payload_sha256: 'f'.repeat(64), parents: ['b'.repeat(64)], execution_authorized: false as const},
    ],
    disposition: 'REVIEW_REQUIRED' as const,
    objective: 'mean_next_session_net_return-minus-endpoint_loss-and-turnover',
    limitations: ['Endpoint loss is not intraday drawdown.', 'Corporate actions are unqualified.'],
    execution_authorized: false as const, exact_external_economics: false as const, live_money: false as const,
  }
}

it.each(['synthetic_fixture', 'user_supplied_unqualified'] as const)('projects persisted %s theory evidence and durable DAG without execution controls', async dataClass => {
  vi.mocked(getQuantLab).mockResolvedValue({
    strategies: [], versions: [], experiments: [], comparisons: [], theory_campaigns: [campaign(dataClass)],
    evidence_classes: ['BACKTEST'], execution: {state: 'NOT_OBSERVED', environment: 'BROKER PAPER'},
    live_money: false, execution_authorized: false,
  })
  render(<QuantPage />)
  await waitFor(() => expect(screen.getByText('Compare strategies on frozen inputs')).toBeTruthy())
  expect(screen.getByText(dataClass === 'synthetic_fixture' ? 'SYNTHETIC BACKTEST' : 'SIMULATED EXECUTION · USER-SUPPLIED UNQUALIFIED')).toBeTruthy()
  expect(screen.getByText('Independent next-session open → close roundtrips')).toBeTruthy()
  expect(screen.getByText(/Candidates: 2/)).toBeTruthy()
  expect(screen.getByText('1.20%')).toBeTruthy()
  expect(screen.getByText('0.10%')).toBeTruthy()
  expect(screen.getAllByText('3.00%')).toHaveLength(2)
  expect(screen.getAllByText('1.98×')).toHaveLength(2)
  expect(screen.getByText('Endpoint loss is not intraday drawdown.')).toBeTruthy()
  expect(screen.getByText(/"top_n": 2/)).toBeTruthy()
  expect(screen.getByText(`runstate:quant_provenance/${'c'.repeat(64)}`)).toBeTruthy()
  expect(screen.getByRole('link', {name: 'b'.repeat(64)}).getAttribute('href')).toBe(`#theory-${'a'.repeat(64)}-${'b'.repeat(64)}`)
  expect(screen.queryByRole('button', {name: /submit|approve|run|optimize/i})).toBeNull()
})

it('keeps incomplete holdout metrics unavailable', async () => {
  const record: import('../lib/types').QuantTheoryCampaign = campaign('synthetic_fixture')
  record.disposition = 'HOLDOUT_INCOMPLETE'
  record.holdout.candidate = {...record.holdout.candidate, disposition: 'INCOMPLETE', scored_period_count: 2,
    mean_net_return: null, max_drawdown: null, turnover: null, fees: null}
  vi.mocked(getQuantLab).mockResolvedValue({
    strategies: [], versions: [], experiments: [], comparisons: [], theory_campaigns: [record],
    evidence_classes: ['BACKTEST'], execution: {state: 'NOT_OBSERVED', environment: 'BROKER PAPER'},
    live_money: false, execution_authorized: false,
  })
  render(<QuantPage />)
  await waitFor(() => expect(screen.getByText(/HOLDOUT_INCOMPLETE/)).toBeTruthy())
  expect(screen.getByText('2/3 · INCOMPLETE')).toBeTruthy()
  expect(screen.getAllByText('unavailable')).toHaveLength(3)
})

it('follows a DAG parent without replacing the hash route', async () => {
  vi.mocked(getQuantLab).mockResolvedValue({
    strategies: [], versions: [], experiments: [], comparisons: [], theory_campaigns: [campaign('synthetic_fixture')],
    evidence_classes: ['BACKTEST'], execution: {state: 'NOT_OBSERVED', environment: 'BROKER PAPER'},
    live_money: false, execution_authorized: false,
  })
  window.location.hash = '#/quant'
  render(<QuantPage />)
  await waitFor(() => expect(screen.getByText('Compare strategies on frozen inputs')).toBeTruthy())
  const target = document.getElementById(`theory-${'a'.repeat(64)}-${'b'.repeat(64)}`)!
  target.scrollIntoView = vi.fn()
  fireEvent.click(screen.getByRole('link', {name: 'b'.repeat(64)}))
  expect(target.scrollIntoView).toHaveBeenCalledWith({block: 'center'})
  expect(document.activeElement).toBe(target)
  expect(window.location.hash).toBe('#/quant')
})
