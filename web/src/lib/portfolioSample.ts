// A known `trex-portfolio/1` document for tests and the local fixture server
// (never imported by the app). Shapes follow the Wave 2 payload
// (src/tree_options/trex_web/portfolio_view.py); the unavailable twin is the
// shared fixture tests/contracts/fixtures/wave3/wave2_portfolio_unavailable.json.

import type { PortfolioView } from './contracts'

export function samplePortfolio(): PortfolioView {
  return {
    schema: 'trex-portfolio/1',
    now: '2026-10-22T14:02:00Z',
    account_ids: ['DU0000001'],
    holdings: {
      status: 'ok',
      reason: null,
      account_id: 'DU0000001',
      as_of: '2026-10-22T14:01:00Z',
      age_s: 60,
      ledger_cash_micros: 24_987_650_000,
      rows: [
        {
          account_id: 'DU0000001',
          con_id: 756733,
          sec_type: 'STK',
          symbol: 'SPY',
          quantity: 3,
          cost_basis_micros: 2_014_440_000,
          lots: [
            {
              attribution: {
                intent_id: 'eq-spy-001',
                strategy_version: 'equity-pilot/1',
                program_id: 'prog-equity-pilot',
                sleeve_ref: 'sleeve-equity-01',
              },
              quantity: 2,
            },
            {
              attribution: { intent_id: null, strategy_version: null, program_id: null, sleeve_ref: 'unassigned' },
              quantity: 1,
            },
          ],
        },
        {
          account_id: 'DU0000001',
          con_id: 712345679,
          sec_type: 'OPT',
          symbol: 'SPY',
          quantity: -1,
          cost_basis_micros: -20_000_000,
          lots: [
            {
              attribution: {
                intent_id: 'opt-qsl-008',
                strategy_version: 'qsl/3',
                program_id: 'qsl-observation',
                sleeve_ref: 'sleeve-options-01',
              },
              quantity: -1,
            },
          ],
        },
      ],
    },
    cash: { status: 'stale', reason: 'risk_snapshot_stale', settled_cash_cents: 2_500_000, as_of: '2026-10-22T13:57:00Z', age_s: 300 },
    risk: {
      status: 'stale',
      reason: 'risk_snapshot_stale',
      account_id: 'DU0000001',
      as_of: '2026-10-22T13:57:00Z',
      age_s: 300,
      stale_after_s: 120,
      open_loss_cents: 4_700,
      day_realized_loss_cents: 0,
      equity_gross_cents: 201_450,
    },
    outside_positions: {
      status: 'stale',
      reason: 'risk_snapshot_stale',
      rows: [{ con_id: 320227571, sec_type: 'STK', symbol: 'QQQ', quantity: 10 }],
    },
    reconciliation: {
      status: 'reconciliation_required',
      reason: 'reconciliation_required',
      fresh: true,
      as_of: '2026-10-22T14:01:30Z',
      age_s: 30,
      mismatches: [
        { con_id: 756733, local_qty: 3, broker_qty: 2, kind: 'quantity_mismatch' },
        { con_id: 712345679, local_qty: -1, broker_qty: 0, kind: 'missing' },
      ],
    },
    kill_files: { PAUSE_ENTRIES: true, HALT: false, FLATTEN: false },
    entry_blockers: ['entries_paused', 'reconciliation_required', 'risk_snapshot_stale'],
    exits_blocked: false,
    problems: [],
    execution_enabled: false,
  }
}
