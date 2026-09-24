import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SymbolIdeas } from '../../lib/types'
import { getSymbolIdeas } from '../../lib/api'
import { IdeasPanel } from './IdeasPanel'

vi.mock('../../lib/api', () => ({
  getSymbolIdeas: vi.fn(),
}))
const mocked = vi.mocked(getSymbolIdeas)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const base: SymbolIdeas = {
  now: '2026-09-23T19:00:00-04:00',
  symbol: 'TEST',
  signals: {
    session: '2026-09-23',
    age_seconds: 300,
    xsmom: {
      score: 1.42,
      in_top3: true,
      top3: ['TEST', 'AAPL', 'MSFT'],
      is_rebalance_day: true,
      n_ranked: 48,
      conventions_agree: true,
    },
    pead: {
      beats: [{ report_date: '2026-09-03', move: 0.042 }],
      evaluated: [
        {
          report_date: '2026-08-06',
          prior_session: '2026-08-05',
          move: 0.031,
          fires: true,
          reason: 'beat + drift window open',
        },
      ],
    },
    next_report: '2026-12-03',
  },
  queue: {
    session: '2026-09-23',
    entry_session: '2026-09-24',
    valid_until: '2026-09-25',
    miner_status: 'PROPOSED',
    deals: [
      {
        deal_id: 'd-1',
        rank: 1,
        status: 'PROPOSED',
        row_title: 'TEST 2026-10-16 100/105 call debit',
        kind: 'call_debit',
        underlying: 'TEST',
        quantity: 1,
        legs: [
          { right: 'C', action: 'buy', strike: 100, expiry: '2026-10-16', bid: 5.1, ask: 5.6, oi: 1200, iv: 0.25, delta: 0.5 },
          { right: 'C', action: 'sell', strike: 105, expiry: '2026-10-16', bid: 2.9, ask: 3.3, oi: 900, iv: 0.24, delta: 0.34 },
        ],
        ref_mid: 2.1,
        fill: null,
        limit: 2.4,
        max_loss: 240,
        signal: { name: 'xsmom_top3', excess_20: 0.031 },
        reasons: ['top-3 xsmom score', 'liquidity ok'],
        notes: ['advisory only'],
      },
    ],
  },
  paper_positions: [
    {
      plan_id: 'plan-7',
      structure_id: 'plan-7:s1',
      account_mode: 'paper',
      expiry: '2026-10-16',
      long_strike: 100,
      short_strike: 105,
      quantity: 1,
      open_qty: 1,
      status: 'open',
      entry_fill: 2.15,
      exit_deadline: '2026-10-09',
    },
  ],
  cards: {
    lines: ['2026-09-23 TEST held', 'survivor: none'],
    ledger_sha256_12: 'abc123def456',
  },
  research: {
    ledger_date: '2026-09-23',
    sha256_12: 'fed654cba321',
    entries: [{ section: 'VRP', line: 'vrp withheld: schedule incomplete' }],
  },
  protocol: {
    allowed_direction: ['xsmom_top3', 'pead_beat'],
    context_only: ['card_history', 'research'],
    advisory: true,
  },
}

describe('IdeasPanel', () => {
  it('renders the protocol boundary copy verbatim', async () => {
    mocked.mockResolvedValue(base)
    render(<IdeasPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('xsmom')).toBeTruthy())

    expect(screen.getByText('advisory — the desk suggests, the operator decides')).toBeTruthy()
    expect(screen.getByText('selection rule PROPOSED — pending operator ruling')).toBeTruthy()
    expect(screen.getByText('paper · not executed')).toBeTruthy()
    expect(screen.getByText('sealed scratch lane · not the desk protocol')).toBeTruthy()
    expect(screen.getByText('research state · non-directional')).toBeTruthy()
    expect(
      screen.getByText(
        'survivors are actionable-as-information only; nothing here points a trade — only ALLOWED_DIRECTION signals may.',
      ),
    ).toBeTruthy()
  })

  it('derives direction chips from protocol.allowed_direction, never hardcoding', async () => {
    mocked.mockResolvedValue(base)
    render(<IdeasPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('xsmom')).toBeTruthy())
    // in_top3 AND a fired beat, both allowed -> both chips
    expect(screen.getByText('xsmom_top3 · ALLOWED_DIRECTION')).toBeTruthy()
    expect(screen.getByText('pead_beat · ALLOWED_DIRECTION')).toBeTruthy()

    // not in top3, no beat fired, families not allowed -> no chips at all
    cleanup()
    mocked.mockResolvedValue({
      ...base,
      signals: {
        ...base.signals!,
        xsmom: { ...base.signals!.xsmom, in_top3: false, top3: ['AAPL', 'MSFT', 'NVDA'] },
        pead: { beats: [], evaluated: [] },
      },
      protocol: { allowed_direction: [], context_only: [], advisory: true },
    })
    render(<IdeasPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('xsmom')).toBeTruthy())
    expect(screen.queryByText('xsmom_top3 · ALLOWED_DIRECTION')).toBeNull()
    expect(screen.queryByText('pead_beat · ALLOWED_DIRECTION')).toBeNull()

    // in_top3 but the family is NOT allowed -> still no chip (the boundary)
    cleanup()
    mocked.mockResolvedValue({
      ...base,
      signals: {
        ...base.signals!,
        pead: { beats: [], evaluated: [] },
      },
      protocol: { allowed_direction: ['pead_beat'], context_only: ['xsmom_top3'], advisory: true },
    })
    render(<IdeasPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('xsmom')).toBeTruthy())
    expect(screen.queryByText('xsmom_top3 · ALLOWED_DIRECTION')).toBeNull()
  })

  it('renders signals: score, top3 badges, rebalance note, pead events', async () => {
    mocked.mockResolvedValue(base)
    render(<IdeasPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('1.42')).toBeTruthy())
    expect(screen.getByText(/ranked over 48 names/)).toBeTruthy()
    expect(screen.getByText(/rebalance day — ranks recompute today/)).toBeTruthy()
    // the name carries the highlighted badge inside the top3 row
    expect(screen.getByText('TEST', { selector: '.badge-open' })).toBeTruthy()
    expect(screen.getByText('AAPL', { selector: '.badge-planned' })).toBeTruthy()
    // pead: beats + latest evaluated with its reason
    expect(screen.getByText('beat 2026-09-03 · +4.2%')).toBeTruthy()
    expect(
      screen.getByText(
        'evaluated 2026-08-06 (prior 2026-08-05) · move +3.1% · fires — beat + drift window open',
      ),
    ).toBeTruthy()
    expect(screen.getByText('next report 2026-12-03')).toBeTruthy()
  })

  it('renders queue deals: legs table, economics kv, signal, reasons', async () => {
    mocked.mockResolvedValue(base)
    render(<IdeasPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('#1 TEST 2026-10-16 100/105 call debit')).toBeTruthy())
    expect(screen.getByText(/session 2026-09-23 · entry 2026-09-24 · valid until 2026-09-25/)).toBeTruthy()
    expect(screen.getByText('buy')).toBeTruthy()
    expect(screen.getByText('5.60')).toBeTruthy()
    expect(screen.getByText('1,200')).toBeTruthy()
    expect(screen.getByText('25.0%')).toBeTruthy()
    expect(screen.getByText('max loss')).toBeTruthy()
    expect(screen.getByText('240.00')).toBeTruthy()
    expect(screen.getByText('signal xsmom_top3 · excess20 0.03')).toBeTruthy()
    expect(screen.getByText('top-3 xsmom score')).toBeTruthy()
  })

  it('links paper positions to their plans and marks them not executed', async () => {
    mocked.mockResolvedValue(base)
    render(<IdeasPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('paper · not executed')).toBeTruthy())
    const link = screen.getByRole('link', { name: 'plan-7' })
    expect(link.getAttribute('href')).toBe('#/plan/plan-7')
    expect(screen.getByText('plan-7:s1')).toBeTruthy()
    expect(screen.getByText('2.15')).toBeTruthy()
  })

  it('renders card history as sealed plain text and research context grouped', async () => {
    mocked.mockResolvedValue(base)
    render(<IdeasPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('sealed scratch lane · not the desk protocol')).toBeTruthy())
    expect(screen.getByText(/scratch ledger abc123def456/)).toBeTruthy()
    expect(screen.getByText(/2026-09-23 TEST held survivor: none/)).toBeTruthy()
    expect(screen.getByText('VRP')).toBeTruthy()
    expect(screen.getByText('vrp withheld: schedule incomplete')).toBeTruthy()
    expect(screen.getByText(/ledger 2026-09-23 · fed654cba321/)).toBeTruthy()
  })

  it('renders honest empty states for null sections', async () => {
    mocked.mockResolvedValue({
      ...base,
      signals: null,
      queue: null,
      paper_positions: [],
      cards: null,
      research: null,
    })
    render(<IdeasPanel sym="TEST" />)
    await waitFor(() =>
      expect(screen.getByText('advisory — the desk suggests, the operator decides')).toBeTruthy(),
    )
    expect(screen.getByText('no signals yet for TEST')).toBeTruthy()
    expect(
      screen.getByText(/no queue yet — the miner's selection rule is pending an operator ruling/),
    ).toBeTruthy()
    expect(screen.getByText('no paper positions')).toBeTruthy()
    expect(screen.getByText('no card history yet')).toBeTruthy()
    expect(screen.getByText('no research ledger context')).toBeTruthy()
  })

  it('shows the unreachable-surface card before the ideas endpoint exists', async () => {
    mocked.mockRejectedValue(new Error('fetch api/market/TEST/ideas failed: 404'))
    render(<IdeasPanel sym="TEST" />)
    await waitFor(() =>
      expect(
        screen.getByText(/Ideas surface not reachable yet\. fetch api\/market\/TEST\/ideas failed: 404/),
      ).toBeTruthy(),
    )
  })
})
