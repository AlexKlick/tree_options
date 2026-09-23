import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { WatchProposal } from '../lib/types'
import { decideProposal, requestProposals } from '../lib/api'
import { ProposalsCard } from './ProposalsCard'

vi.mock('../lib/api', () => ({ decideProposal: vi.fn(), requestProposals: vi.fn() }))
const mockedDecide = vi.mocked(decideProposal)
const mockedAsk = vi.mocked(requestProposals)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const prop: WatchProposal = {
  id: 'a1',
  symbol: 'TSM',
  action: 'add',
  rationale: 'semis breadth widening',
  confidence: 0.62,
  status: 'pending',
  created_at: '2026-09-22T19:00:00-04:00',
  provenance: { provider: 'local', model: 'Qwen/Qwen3.8-27B', trigger: 'operator' },
}

describe('ProposalsCard', () => {
  it('shows provenance and decides by proposal id (hidden optimistically)', async () => {
    mockedDecide.mockResolvedValue({ accepted: true, request_id: 'r' })
    const onChanged = vi.fn()
    render(<ProposalsCard proposals={[prop]} lastRun={null} onChanged={onChanged} />)
    expect(screen.getByText('TSM')).toBeTruthy()
    expect(screen.getByText(/62%/)).toBeTruthy()
    expect(screen.getByText('local · Qwen/Qwen3.8-27B')).toBeTruthy()
    expect(screen.getByText('model suggests · you decide')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Approve add TSM' }))
    await waitFor(() => expect(mockedDecide).toHaveBeenCalledWith('approve', 'a1'))
    await waitFor(() => expect(screen.queryByText('TSM')).toBeNull())
    expect(onChanged).toHaveBeenCalled()
  })

  it('asks for ideas and reports a failed last run honestly', async () => {
    mockedAsk.mockResolvedValue({ accepted: true, request_id: 'r' })
    render(
      <ProposalsCard
        proposals={[]}
        lastRun={{
          at: new Date(Date.now() - 5 * 60_000).toISOString(),
          status: 'failed', provider: null, model: null, elapsed_s: null,
          trigger: 'operator', notes: ['local: HTTP 500', 'zai: HTTP 429'], added: 0,
        }}
        onChanged={() => {}}
      />,
    )
    expect(screen.getByText(/last attempt 5m ago failed — local: HTTP 500; zai: HTTP 429/)).toBeTruthy()
    expect(screen.getByText(/No pending proposals/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Ask for ideas' }))
    await waitFor(() => expect(screen.getByText(/runner thinking/)).toBeTruthy())
  })
})
