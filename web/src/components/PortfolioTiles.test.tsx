import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { AccountBlock, PortfolioBlock } from '../lib/types'
import { PortfolioTiles } from './PortfolioTiles'

const portfolio: PortfolioBlock = {
  plans_count: 2,
  plans_with_state: 1,
  open_qty: 8,
  committed_at_caps: 1840,
  committed_filled: 477,
  unrealized_open: -14,
  unrealized_filled: -14,
  realized: null,
  realized_partial_count: 0,
  marks_age_seconds: 12,
  marks_stale: false,
  worst_state: 'open',
  by_account_mode: {},
}

const account: AccountBlock = {
  account_id: 'DUT143714',
  net_liquidation: 1000252.09,
  cash: 999516.91,
  buying_power: 3998067.63,
  currency: 'USD',
  ts: '2026-09-22T16:00:00-04:00',
  age_seconds: 20,
  source: 'putspread-20260922/account.json',
}

describe('PortfolioTiles', () => {
  it('renders portfolio tiles with signed values', () => {
    render(<PortfolioTiles portfolio={portfolio} account={account} />)
    expect(screen.getByText('Open contracts')).toBeTruthy()
    expect(screen.getByText('8')).toBeTruthy()
    expect(screen.getByText('-$14')).toBeTruthy()
    expect(screen.getByText('Net liq (DUT143714)')).toBeTruthy()
    expect(screen.getByText('$1,000,252')).toBeTruthy()
    expect(screen.getByText('$3,998,068')).toBeTruthy()
    expect(screen.getByText('● marks 12s ago')).toBeTruthy()
  })

  it('hides the account card when there is no account data', () => {
    render(<PortfolioTiles portfolio={portfolio} account={null} />)
    expect(screen.queryByText(/Net liq/)).toBeNull()
    expect(screen.getByText('Open contracts')).toBeTruthy()
  })

  it('marks staleness shows the stale pill', () => {
    render(
      <PortfolioTiles
        portfolio={{ ...portfolio, marks_stale: true, marks_age_seconds: 900 }}
        account={null}
      />,
    )
    expect(screen.getByText('● marks 15m ago')).toBeTruthy()
  })

  it('em dash when nothing has filled yet', () => {
    render(
      <PortfolioTiles portfolio={{ ...portfolio, unrealized_open: null }} account={null} />,
    )
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(1)
  })
})
