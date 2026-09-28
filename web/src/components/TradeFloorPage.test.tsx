import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { getTradeFloorReplays } from '../lib/api'
import { TradeFloorPage } from './TradeFloorPage'

vi.mock('../lib/api', () => ({
  getTradeFloorReplays: vi.fn(),
  getGateway: () => new Promise(() => {}),
  getExitMachine: () => new Promise(() => {}),
}))

afterEach(() => { cleanup(); vi.clearAllMocks() })

it('plays agent choices, reveals modeled marks, and never offers broker controls', async () => {
  vi.mocked(getTradeFloorReplays).mockResolvedValue({
    schema: 'desk-trade-floor-list/1', execution_enabled: false, replays: [{
      schema: 'desk-trade-floor-replay/1', id: 'run-one', source_head: 'a'.repeat(40),
      source_manifest_sha256: 'b'.repeat(64), sample_manifest_sha256: 'c'.repeat(64),
      provider_manifest_sha256: 'd'.repeat(64), replay_manifest_sha256: 'e'.repeat(64),
      starting_capital: '5000', excluded_calibration_snapshot: 'w1-01',
      execution_enabled: false, research_only: true,
      limitations: ['Trade bars are not fills.'],
      windows: [{ id: 'w1', start: '2026-01-01', end: '2026-03-31', series: 6,
        traded_minute_bars: 100, rounds: 2, final_scores: [
          { id: 'zai', label: 'Z.ai', entered: 1, wins: 1, losses: 0, closed_capital_proxy: '5010' },
          { id: 'flash', label: 'Z.ai Flash', entered: 0, wins: 0, losses: 0, closed_capital_proxy: '5000' },
          { id: 'minimax', label: 'MiniMax', entered: 0, wins: 0, losses: 0, closed_capital_proxy: '5000' },
        ] }],
      rounds: [
        { id: 'w1-02', window: 'w1', snapshot_id: 's:2026-01-02T10:00',
          as_of: '2026-01-02T15:00:00+00:00', all_as_of_candidates: 1,
          candidates: [{ id: 'candidate-one', symbol: 'SPY', structure: 'put_credit',
            dte: 20, width: '1', premium_proxy: '0.4', max_loss_proxy: '60',
            max_gain_proxy: '40', reward_to_risk_proxy: '0.67' }],
          traders: [
            { id: 'zai', label: 'Z.ai', selected_id: 'candidate-one', action: 'entered',
              action_reason: 'selected', model_reason: 'bounded risk',
              entry_loss_proxy: '65', eventual_pnl_proxy: '10' },
            { id: 'flash', label: 'Z.ai Flash', selected_id: null, action: 'skip',
              action_reason: 'no_selection', model_reason: 'skip',
              entry_loss_proxy: null, eventual_pnl_proxy: null },
            { id: 'minimax', label: 'MiniMax', selected_id: null, action: 'skip',
              action_reason: 'no_selection', model_reason: 'skip',
              entry_loss_proxy: null, eventual_pnl_proxy: null },
          ] },
        { id: 'w1-03', window: 'w1', snapshot_id: 's:2026-01-16T10:00',
          as_of: '2026-01-16T15:00:00+00:00', all_as_of_candidates: 0,
          candidates: [], traders: [
            { id: 'zai', label: 'Z.ai', selected_id: null, action: 'skip',
              action_reason: 'no_selection', model_reason: '',
              entry_loss_proxy: null, eventual_pnl_proxy: null },
            { id: 'flash', label: 'Z.ai Flash', selected_id: null, action: 'skip',
              action_reason: 'no_selection', model_reason: '',
              entry_loss_proxy: null, eventual_pnl_proxy: null },
            { id: 'minimax', label: 'MiniMax', selected_id: null, action: 'skip',
              action_reason: 'no_selection', model_reason: '',
              entry_loss_proxy: null, eventual_pnl_proxy: null },
          ] },
      ],
    }],
  })
  render(<TradeFloorPage />)
  await waitFor(() => expect(screen.getByText('Three model traders. One market clock.')).toBeTruthy())
  expect(screen.getAllByText('Outcome hidden')).toHaveLength(3)
  expect(screen.queryByText('+$10.00')).toBeNull()
  expect(screen.getByText('No broker connection')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Reveal outcome' }))
  expect(screen.getByText('+$10.00')).toBeTruthy()
  expect(screen.getByText('$5,010.00')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Next round' }))
  expect(screen.getByText(/No candidate passed the historical as-of filters/)).toBeTruthy()
  expect(screen.queryByRole('button', { name: /submit|place order|approve/i })).toBeNull()
})
