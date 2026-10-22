import { render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { reasonText } from '../lib/activityApi'
import type { PortfolioView } from '../lib/contracts'
import * as api from '../lib/portfolioApi'
import { centsText, microsText } from '../lib/portfolioApi'
import { samplePortfolio } from '../lib/portfolioSample'
import { PortfolioPage } from './PortfolioPage'

vi.mock('../lib/portfolioApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/portfolioApi')>()),
  getPortfolio: vi.fn(),
}))
afterEach(() => vi.resetAllMocks())

const wave3 = import.meta.glob('../../../tests/contracts/fixtures/wave3/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>
const unavailable = (): PortfolioView =>
  structuredClone(wave3['../../../tests/contracts/fixtures/wave3/wave2_portfolio_unavailable.json']) as PortfolioView

const region = (name: string) => screen.getByRole('region', { name })

it('shows every unreadable block as unavailable with its reason, never as zero', async () => {
  vi.mocked(api.getPortfolio).mockResolvedValue(unavailable())
  render(<PortfolioPage />)
  await screen.findByRole('region', { name: 'Cash' })
  expect(within(region('Cash')).getAllByText(reasonText('risk_snapshot_missing'), { exact: false }).length).toBeGreaterThan(0)
  expect(within(region('Holdings')).getByText(reasonText('holdings_missing'), { exact: false })).toBeTruthy()
  expect(within(region('Reconciliation')).getByText(reasonText('reconciliation_missing'), { exact: false })).toBeTruthy()
  expect(within(region('Positions outside TREX')).getByText(reasonText('risk_snapshot_missing'), { exact: false })).toBeTruthy()
  expect(within(region('Risk snapshot')).getByText(reasonText('risk_snapshot_missing'), { exact: false })).toBeTruthy()
  // no amount, count or age is invented for an unknown fact
  expect(document.body.textContent).not.toMatch(/\$\d/)
  expect(document.body.textContent).not.toMatch(/\b0 positions?\b/)
  const blockers = region('What blocks new entries')
  expect(within(blockers).getByText(reasonText('reconciliation_required'), { exact: false })).toBeTruthy()
  expect(within(blockers).getByText(reasonText('risk_snapshot_stale'), { exact: false })).toBeTruthy()
  expect(screen.getByText(/Exits run automatically/)).toBeTruthy()
  expect(screen.getByText(/No account recorded yet/)).toBeTruthy()
})

it('shows holdings, cash, mismatches, outside positions and the risk snapshot age', async () => {
  const view = samplePortfolio()
  vi.mocked(api.getPortfolio).mockResolvedValue(view)
  render(<PortfolioPage />)
  const holdings = await screen.findByRole('region', { name: 'Holdings' })
  const spy = view.holdings.rows![0]
  expect(within(holdings).getByText(microsText(spy.cost_basis_micros), { exact: false })).toBeTruthy()
  expect(within(holdings).getByText(/2 TREX, 1 outside TREX/)).toBeTruthy()
  expect(within(holdings).getByText(spy.lots[0].attribution.program_id!, { exact: false })).toBeTruthy()

  const cash = region('Cash')
  expect(within(cash).getByText(centsText(view.cash.settled_cash_cents!))).toBeTruthy()
  expect(within(cash).getByText(microsText(view.holdings.ledger_cash_micros!))).toBeTruthy()

  const risk = region('Risk snapshot')
  expect(within(risk).getByText(/Stale/)).toBeTruthy()
  expect(within(risk).getByText(/5m old/)).toBeTruthy()
  expect(within(risk).getByText(/stale after 2m/)).toBeTruthy()
  expect(within(risk).getByText(centsText(view.risk.open_loss_cents!))).toBeTruthy()

  const rec = region('Reconciliation')
  expect(within(rec).getByText(/do not match/)).toBeTruthy()
  const rows = within(rec).getAllByRole('row')
  expect(rows).toHaveLength(1 + view.reconciliation.mismatches!.length)
  expect(within(rows[1]).getByText('SPY')).toBeTruthy()
  expect(within(rows[1]).getByText('3')).toBeTruthy()
  expect(within(rows[1]).getByText('2')).toBeTruthy()

  const outside = region('Positions outside TREX')
  expect(within(outside).getByText('QQQ')).toBeTruthy()
  expect(within(outside).getByText(/TREX never trades or closes these/)).toBeTruthy()

  expect(screen.getByText(/New entries are paused/)).toBeTruthy()
  expect(screen.getByText(/Exits run automatically/)).toBeTruthy()
  expect(screen.getByText(/DU0000001/)).toBeTruthy()
})

it('raises a non-paper account and a standing emergency stop', async () => {
  const view = samplePortfolio()
  vi.mocked(api.getPortfolio).mockResolvedValue({
    ...view,
    account_ids: ['DU0000001', 'U7654321'],
    problems: [{ reason: 'non_paper_account', account_id: 'U7654321' }],
    kill_files: { PAUSE_ENTRIES: false, HALT: true, FLATTEN: false },
    exits_blocked: true,
  })
  render(<PortfolioPage />)
  const alerts = await screen.findAllByRole('alert')
  expect(alerts.map((a) => a.textContent).join(' ')).toMatch(/U7654321.*not a paper account/)
  expect(screen.getByText(/Exits are stopped/)).toBeTruthy()
  expect(screen.getByText(/Emergency stop \(also stops exits\)/)).toBeTruthy()
})

it('shows a failed read as an error, not as an empty portfolio', async () => {
  vi.mocked(api.getPortfolio).mockRejectedValue(new Error('fetch api/portfolio failed: 503'))
  render(<PortfolioPage />)
  expect((await screen.findByRole('alert')).textContent).toContain('503')
  expect(screen.queryByRole('region', { name: 'Holdings' })).toBeNull()
  expect(document.body.textContent).not.toMatch(/\$\d/)
})
