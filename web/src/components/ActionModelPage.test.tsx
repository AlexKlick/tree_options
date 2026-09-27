import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ActionModelPage } from './ActionModelPage'
import { getActionModelExample, getHistoricalReplays, getPlans } from '../lib/api'

vi.mock('../lib/api', () => ({
  getActionModelExample: vi.fn(),
  getPlans: vi.fn(),
  getHistoricalReplays: vi.fn(),
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
    schema: 'desk-historical-replay-list/1', execution_enabled: false, reports: [],
  })
  render(<ActionModelPage />)
  await waitFor(() => expect(screen.getByText('Compare a version')).toBeTruthy())
  expect(screen.getByText(/execution authorized: no/)).toBeTruthy()
  expect(screen.getByText(/Governed entry: disabled/)).toBeTruthy()
  expect(screen.getByText(/No historical replay has completed/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: /N01.*Resolve inputs/ }))
  expect(screen.getByText(/Guards: input_hash_match/)).toBeTruthy()
  expect(screen.getByText(/No attempt, grant, permit, broker effect/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: /submit|approve|trade/i })).toBeNull()
})
