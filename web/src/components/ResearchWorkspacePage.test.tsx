import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from '../lib/workspaceApi'
import { ResearchWorkspacePage } from './ResearchWorkspacePage'

vi.mock('../lib/workspaceApi', () => ({
  getResearchDatasets: vi.fn(), getResearchJobs: vi.fn(), getResearchJob: vi.fn(),
  assignResearch: vi.fn(), controlResearchJob: vi.fn(), getPaperAccounts: vi.fn(),
  getPaperDeployments: vi.fn(), getPaperAllocations: vi.fn(), createPaperAllocation: vi.fn(),
  proposePaperDeployment: vi.fn(), haltPaperDeployment: vi.fn(), bindPaperSleeve: vi.fn(),
}))
afterEach(() => { cleanup(); vi.clearAllMocks() })
beforeEach(() => {
  vi.mocked(api.getResearchDatasets).mockResolvedValue({datasets: [], controls_enabled: true})
  vi.mocked(api.getResearchJobs).mockResolvedValue({jobs: [], controls_enabled: true})
  vi.mocked(api.getPaperAccounts).mockResolvedValue({accounts: [], setup_required: true, blockers: ['paper_account_not_connected'], controls_enabled: true})
  vi.mocked(api.getPaperDeployments).mockResolvedValue({deployments: []})
  vi.mocked(api.getPaperAllocations).mockResolvedValue({plans: []})
})

it('shows account setup and blocks research without a frozen dataset', async () => {
  render(<ResearchWorkspacePage />)
  await screen.findByText('No frozen research datasets registered.')
  expect((screen.getByRole('button', {name: 'Assign research'}) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText('Qualify your existing IBKR paper account first. Alpaca Paper through SnapTrade is also supported.')).toBeTruthy()
  expect(screen.queryByRole('button', {name: /submit order|trade now|approve/i})).toBeNull()
})

it('queues a bounded assignment with an explicit synthetic data label', async () => {
  vi.mocked(api.getResearchDatasets).mockResolvedValue({datasets: [{dataset_id: 'fixture', label: 'Machinery fixture', data_class: 'synthetic_fixture', promotion_allowed: false}], controls_enabled: true})
  vi.mocked(api.assignResearch).mockResolvedValue({run_id: 'job-one', status: 'queued', hypothesis: 'Compare momentum against equal weight', dataset_id: 'fixture', capital: '50000', max_candidates: 8, generations: 1, strategy_id: 'momentum_12_1', top_n: null, reflect_glm53: false, data_class: 'synthetic_fixture'})
  render(<ResearchWorkspacePage />)
  await screen.findByRole('option', {name: /Machinery fixture/})
  fireEvent.change(screen.getByLabelText('Frozen dataset'), {target: {value: 'fixture'}})
  fireEvent.change(screen.getByLabelText('Research hypothesis'), {target: {value: 'Compare momentum against equal weight'}})
  fireEvent.change(screen.getByLabelText('Strategy family'), {target: {value: 'momentum_12_1'}})
  fireEvent.click(screen.getByRole('button', {name: 'Assign research'}))
  await waitFor(() => expect(api.assignResearch).toHaveBeenCalledWith(expect.objectContaining({dataset_id: 'fixture', capital: '50000', strategy_id: 'momentum_12_1', max_candidates: 8, reflect_glm53: false})))
  expect(api.proposePaperDeployment).not.toHaveBeenCalled()
  const evidenceLabel = screen.getByText('SYNTHETIC BACKTEST', {selector: 'strong'})
  expect(evidenceLabel.closest('p')?.textContent).toContain('Machinery fixture')
  expect(screen.getByLabelText('Frozen dataset').getAttribute('aria-describedby')).toBe(evidenceLabel.closest('p')?.id)
})

it('retains the requested 29 sleeve plan and separate capital scales', async () => {
  const sleeves = [...Array.from({length: 19}, (_, i) => ({sleeve_id: `large-${i}`, label: `Large ${i + 1}`, capital_usd: '50000', account_alias: null, reserved_usd: '0'})), ...Array.from({length: 10}, (_, i) => ({sleeve_id: `small-${i}`, label: `Small ${i + 1}`, capital_usd: '5000', account_alias: null, reserved_usd: '0'}))]
  vi.mocked(api.getPaperAllocations).mockResolvedValue({plans: [{plan_id: 'plan-one', total_capital_usd: '1000000', sleeves, execution_authorized: false, live_money: false}]})
  render(<ResearchWorkspacePage />)
  await screen.findByText('Large 19')
  expect(screen.getByText('Small 10')).toBeTruthy()
  expect(screen.getAllByText('$50,000').length).toBeGreaterThanOrEqual(19)
  expect(screen.getAllByText('$5,000').length).toBeGreaterThanOrEqual(10)
  expect(screen.getByText(/Shared broker positions require fill attribution/)).toBeTruthy()
})

it('disables mutations when operator controls are off', async () => {
  vi.mocked(api.getResearchDatasets).mockResolvedValue({datasets: [], controls_enabled: false})
  vi.mocked(api.getResearchJobs).mockResolvedValue({jobs: [], controls_enabled: false})
  vi.mocked(api.getPaperAccounts).mockResolvedValue({accounts: [], setup_required: true, blockers: [], controls_enabled: false})
  render(<ResearchWorkspacePage />)
  await screen.findByText(/Operator controls are disabled/)
  expect((screen.getByRole('button', {name: 'Create allocation plan'}) as HTMLButtonElement).disabled).toBe(true)
})

it('shows failed research distinctly and stop survives the browser session', async () => {
  const job = {run_id: 'job-one', status: 'running', hypothesis: 'Explore frozen momentum', dataset_id: 'fixture', capital: '5000', max_candidates: 4, generations: 1, strategy_id: 'momentum_12_1', top_n: null, reflect_glm53: false, data_class: 'synthetic_fixture'}
  vi.mocked(api.getResearchJobs).mockResolvedValue({jobs: [job], controls_enabled: true})
  vi.mocked(api.controlResearchJob).mockResolvedValue({job: {...job, status: 'stopping'}})
  render(<ResearchWorkspacePage />)
  await screen.findByText('Explore frozen momentum')
  fireEvent.click(screen.getByRole('button', {name: 'Stop research job-one'}))
  await waitFor(() => expect(api.controlResearchJob).toHaveBeenCalledWith('job-one', 'stop'))
  expect(api.haltPaperDeployment).not.toHaveBeenCalled()
})

it('refuses allocation mutations when its projection is unavailable', async () => {
  vi.mocked(api.getPaperAllocations).mockRejectedValue(new Error('allocation read failed'))
  render(<ResearchWorkspacePage />)
  await screen.findByText(/allocation read failed/)
  expect((screen.getByRole('button', {name: 'Create allocation plan'}) as HTMLButtonElement).disabled).toBe(true)
})

it('clears an earlier account choice when selecting an unbound sleeve', async () => {
  vi.mocked(api.getPaperAccounts).mockResolvedValue({accounts: [{account_alias: 'paper-a', configured: true, qualification_status: 'BLOCKED', assessed_at: null, expires_at: null, owner_held: false, blockers: []}], setup_required: true, blockers: [], controls_enabled: true})
  vi.mocked(api.getPaperAllocations).mockResolvedValue({plans: [{plan_id: 'plan-one', total_capital_usd: '1000000', sleeves: [{sleeve_id: 'one', label: 'Experiment 01', capital_usd: '50000', account_alias: null, reserved_usd: '0'}], execution_authorized: false, live_money: false}]})
  render(<ResearchWorkspacePage />)
  await screen.findByRole('option', {name: /paper-a/})
  fireEvent.change(screen.getByLabelText('Paper account'), {target: {value: 'paper-a'}})
  fireEvent.click(screen.getByRole('button', {name: 'Use Experiment 01'}))
  expect((screen.getByLabelText('Paper account') as HTMLSelectElement).value).toBe('')
  expect((screen.getByRole('button', {name: 'Create paper proposal'}) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByLabelText('Strategy version').tagName).toBe('SELECT')
})

it('records a paper proposal with the bounded controls and never submits an order', async () => {
  vi.mocked(api.getPaperAccounts).mockResolvedValue({accounts: [{account_alias: 'paper-a', configured: true, qualification_status: 'BLOCKED', assessed_at: null, expires_at: null, owner_held: false, blockers: []}], setup_required: true, blockers: [], controls_enabled: true})
  vi.mocked(api.getPaperAllocations).mockResolvedValue({plans: [{plan_id: 'plan-one', total_capital_usd: '1000000', sleeves: [{sleeve_id: 'one', label: 'Experiment 01', capital_usd: '50000', account_alias: 'paper-a', reserved_usd: '0'}], execution_authorized: false, live_money: false}]})
  vi.mocked(api.proposePaperDeployment).mockResolvedValue({deployment_id: 'review-one', account_alias: 'paper-a', strategy_version: 'operational-canary/1', research_job_id: null, sleeve_id: 'one', intended_capital_usd: '50000', max_gross_notional_usd: '100', max_orders: 1, ttl_seconds: 300, status: 'REVIEW_REQUIRED', execution_status: 'NOT_AUTHORIZED', blockers: [], live_money: false})
  render(<ResearchWorkspacePage />)
  await screen.findByRole('button', {name: 'Use Experiment 01'})
  fireEvent.click(screen.getByRole('button', {name: 'Use Experiment 01'}))
  fireEvent.click(screen.getByRole('button', {name: 'Create paper proposal'}))
  await waitFor(() => expect(api.proposePaperDeployment).toHaveBeenCalledWith(expect.objectContaining({account_alias: 'paper-a', sleeve_id: 'one', strategy_version: 'operational-canary/1', intended_capital_usd: '50000', max_gross_notional_usd: '100', max_orders: 1, ttl_seconds: 300})))
  expect(await screen.findByText('Paper proposal recorded for review. No order submitted.')).toBeTruthy()
  expect(api.assignResearch).not.toHaveBeenCalled()
})

it('provides halt controls for an uncertain deployment without freeing its reservation', async () => {
  const deployment = {deployment_id: 'uncertain-one', account_alias: 'paper-a', strategy_version: 'operational-canary/1', research_job_id: null, intended_capital_usd: '5000', max_gross_notional_usd: '100', max_orders: 1, ttl_seconds: 300, status: 'RECOVERY_REQUIRED', execution_status: 'UNKNOWN_RECONCILIATION_REQUIRED', blockers: ['reconciliation_required'], live_money: false as const}
  vi.mocked(api.getPaperDeployments).mockResolvedValue({deployments: [deployment]})
  vi.mocked(api.haltPaperDeployment).mockResolvedValue({...deployment, status: 'HALTED'})
  render(<ResearchWorkspacePage />)
  await screen.findByText('RECOVERY_REQUIRED · UNKNOWN_RECONCILIATION_REQUIRED')
  fireEvent.click(screen.getByRole('button', {name: 'Halt paper deployment uncertain-one'}))
  await waitFor(() => expect(api.haltPaperDeployment).toHaveBeenCalledWith('uncertain-one'))
  expect(api.createPaperAllocation).not.toHaveBeenCalled()
})


it('distinguishes IBKR read-only qualification from equity execution', async () => {
  vi.mocked(api.getPaperAccounts).mockResolvedValue({accounts: [{account_alias: 'ibkr-paper', provider: 'ibkr', configured: true, qualification_status: 'BLOCKED', assessed_at: null, expires_at: null, owner_held: true, equity_execution_ready: false, blockers: ['ibkr_equity_execution_not_qualified']}], setup_required: true, blockers: [], controls_enabled: true})
  render(<ResearchWorkspacePage />)
  expect(await screen.findByText(/IBKR equity execution: not qualified/)).toBeTruthy()
  expect(screen.getByText('Current runtime ownership must be rechecked before execution.')).toBeTruthy()
  expect(screen.queryByText(/owner (held|not held)/)).toBeNull()
  expect(screen.queryByRole('button', {name: /submit order|trade now/i})).toBeNull()
})

it('shows compact control comparison and selects the actual recorded strategy version', async () => {
  const job = {run_id: 'job-final', status: 'completed', hypothesis: 'Compare a recorded momentum theory', dataset_id: 'fixture', capital: '50000', max_candidates: 4, generations: 0, strategy_id: 'momentum_12_1', top_n: null, reflect_glm53: false, data_class: 'synthetic_fixture'}
  const version = `momentum_12_1/v1/${'a'.repeat(64)}`
  vi.mocked(api.getResearchJobs).mockResolvedValue({jobs: [job], controls_enabled: true})
  vi.mocked(api.getResearchJob).mockResolvedValue({job, result: {disposition: 'NOT_PROMOTABLE', data_class: 'synthetic_fixture', evidence_kind: 'BACKTEST', winner: {strategy_id: 'momentum_12_1', version_id: version}, holdout: {candidate: {mean_net_return: '0.02', fees: '50', period_count: 12}, control: {mean_net_return: '0.01', fees: '45', period_count: 12}}}, provenance: []})
  render(<ResearchWorkspacePage />)
  await screen.findByText(job.hypothesis)
  fireEvent.change(screen.getByLabelText('Research evidence'), {target: {value: job.run_id}})
  await screen.findByText('NOT_PROMOTABLE')
  expect(screen.getByText('2.00%')).toBeTruthy()
  expect(screen.getByText('1.00%')).toBeTruthy()
  expect((screen.getByLabelText('Strategy version') as HTMLSelectElement).value).toBe(version)
  const raw = screen.getByText('Full recorded result').closest('details')
  expect(raw?.hasAttribute('open')).toBe(false)
  expect(api.proposePaperDeployment).not.toHaveBeenCalled()
})
