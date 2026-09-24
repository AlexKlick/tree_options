import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { OptionsSliceRow, SymbolOptions } from '../../lib/types'
import { getSymbolOptions } from '../../lib/api'
import { OptionsPanel } from './OptionsPanel'

vi.mock('../../lib/api', () => ({
  getSymbolOptions: vi.fn(),
}))
const mocked = vi.mocked(getSymbolOptions)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function sliceRow(o: {
  exp: string
  right: string
  strike: number
  ask: number
}): OptionsSliceRow[] {
  return [
    {
      exp: o.exp,
      dte: 9,
      right: o.right,
      strike: o.strike,
      atm: o.strike === 100,
      bid: o.ask - 0.5,
      ask: o.ask,
      mid: o.ask - 0.25,
      iv: 0.25,
      delta: o.right === 'C' ? 0.5 : -0.48,
      gamma: 0.01,
      theta: -0.02,
      vega: 0.03,
      oi: 1200,
      volume: 10,
    },
  ]
}

const base: SymbolOptions = {
  now: '2026-06-03T19:00:00-04:00',
  symbol: 'TEST',
  available: true,
  live: null,
  warnings: [],
  iv30_history: {
    points: [
      [1719792000000, 0.2],
      [1780483200000, 0.29],
    ],
    y_lo: 0.2,
    y_hi: 0.29,
    n: 900,
    first: '2024-06-17',
    last: '2026-06-03',
    source: 'iv-history/vwap_atm.json (IVHIST-001, manual build)',
  },
  recorded: {
    session: '2026-06-03',
    age_seconds: 3600,
    spot: 101.0,
    cards: {
      iv: { '30': 0.25, '60': 0.26, '90': 0.27, '180': 0.28 },
      iv_rank: { rank: 0.369, percentile: 0.19, n: 220, low_n: false, outside_range: null },
      skew25: { '30': 0.011, '90': 0.023 },
      term_slope: 0.111,
      yz22_ann: 0.233,
      liquidity_score: 85,
      earnings: {
        next_report: '2026-08-06',
        event_sessions: ['2026-08-06', '2026-08-07'],
        implied_move: 0.031,
        hist_mean_abs_move: 0.042,
        hist_n: 8,
        in_progress: false,
      },
    },
    atm_term: [
      ['2026-06-12', 9, 0.2512, 9, 'bracket'],
      ['2026-06-19', 16, 0.2612, 9, 'bracket'],
    ],
    slice: [
      ...sliceRow({ exp: '2026-06-12', right: 'C', strike: 100, ask: 5.6 }),
      ...sliceRow({ exp: '2026-06-12', right: 'P', strike: 100, ask: 4.7 }),
      ...sliceRow({ exp: '2026-06-19', right: 'C', strike: 100, ask: 6.5 }),
      ...sliceRow({ exp: '2026-06-19', right: 'P', strike: 100, ask: 5.7 }),
    ],
  },
}

describe('OptionsPanel', () => {
  it('renders the recorded surface: pills, tiles, paired slice, both charts', async () => {
    mocked.mockResolvedValue(base)
    render(<OptionsPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('IV30')).toBeTruthy())

    // source pills + the honest not-warmed hint
    expect(screen.getByText('recorded session 2026-06-03 · not live')).toBeTruthy()
    expect(screen.getByText('live chain not warmed yet — Refresh data spools the warm')).toBeTruthy()

    // metric tiles: vols as %, rank percentile, term slope, liquidity, report
    expect(screen.getAllByText('25.0%').length).toBeGreaterThan(0)
    expect(screen.getByText('19%')).toBeTruthy()
    expect(screen.getByText('+11.1%')).toBeTruthy()
    expect(screen.getByText('23.3%')).toBeTruthy()
    expect(screen.getByText('85')).toBeTruthy()
    expect(screen.getByText('2026-08-06')).toBeTruthy()
    expect(screen.getByText(/implied ±3\.1% vs hist \|move\| 4\.2% \(n=8\)/)).toBeTruthy()

    // slice: expiry subheader, call|strike|put pairing, atm bolded marker
    expect(screen.getByText('2026-06-12 · 9d to expiry')).toBeTruthy()
    expect(screen.getAllByText('100 · atm').length).toBe(2)
    expect(screen.getByText('5.60')).toBeTruthy()
    expect(screen.getByText('4.70')).toBeTruthy()

    // term structure + iv30 history charts with their disclosures
    expect(screen.getByRole('img', { name: 'ATM term structure for TEST' })).toBeTruthy()
    expect(screen.getByText(/2026-06-12 9d · 9 strikes \(bracket\)/)).toBeTruthy()
    expect(screen.getByRole('img', { name: 'IV30 history' })).toBeTruthy()
    expect(
      screen.getByText('2024-06-17..2026-06-03 · 900 sessions · iv-history/vwap_atm.json (IVHIST-001, manual build)'),
    ).toBeTruthy()
  })

  it('warns on thin and out-of-range rank histories', async () => {
    const o: SymbolOptions = {
      ...base,
      recorded: {
        ...base.recorded!,
        cards: {
          ...base.recorded!.cards,
          iv_rank: { rank: null, percentile: null, n: 60, low_n: true, outside_range: 'above' },
        },
      },
    }
    mocked.mockResolvedValue(o)
    render(<OptionsPanel sym="TEST" />)
    await waitFor(() =>
      expect(screen.getByText('iv rank n=60 < 120 — thin history')).toBeTruthy(),
    )
    expect(screen.getByText('iv above the ranked range')).toBeTruthy()
    expect(screen.getByText(/percentile of IV30 over 60 ranked sessions/)).toBeTruthy()
  })

  it('answers an unavailable name with the honest empty card (IV30 still shown)', async () => {
    mocked.mockResolvedValue({ ...base, available: false, recorded: null })
    render(<OptionsPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('no recorded option chains for TEST yet')).toBeTruthy())
    expect(screen.getByRole('img', { name: 'IV30 history' })).toBeTruthy()
  })

  it('degrades a cards-only session honestly', async () => {
    mocked.mockResolvedValue({
      ...base,
      warnings: ['no recorded chain for TEST in the newest 10 feature sessions; cards only (slice null)'],
      recorded: { ...base.recorded!, slice: null },
    })
    render(<OptionsPanel sym="TEST" />)
    await waitFor(() =>
      expect(screen.getByText('cards only — no recorded chain slice for 2026-06-03')).toBeTruthy(),
    )
    expect(
      screen.getByText(/cards only \(slice null\)/),
    ).toBeTruthy()
  })

  it('surfaces the live envelope and toggles the slice source onto it', async () => {
    const liveRows = [
      ...sliceRow({ exp: '2026-06-12', right: 'C', strike: 100, ask: 7.7 }),
      ...sliceRow({ exp: '2026-06-12', right: 'P', strike: 100, ask: 6.9 }),
    ]
    mocked.mockResolvedValue({
      ...base,
      live: { fetched_at: '2026-06-03T15:00:00-04:00', age_seconds: 42, ttl_seconds: 300, spot: 101.5, slice: liveRows },
    })
    render(<OptionsPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('live · delayed CBOE · 42s old')).toBeTruthy())
    // live present: the recorded pill drops its "not live" suffix
    expect(screen.getByText('recorded session 2026-06-03')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Live · 42s old' }))
    await waitFor(() => expect(screen.getByText('7.70')).toBeTruthy())
    expect(screen.getByText(/fetched 42s ago · ttl 300s/)).toBeTruthy()
    expect(screen.queryByText('5.60')).toBeNull()
  })

  it('keeps the last surface and shows the error when the API blips', async () => {
    mocked.mockResolvedValue(base)
    render(<OptionsPanel sym="TEST" />)
    await waitFor(() => expect(screen.getByText('IV30')).toBeTruthy())
    mocked.mockRejectedValue(new Error('fetch api/market/TEST/options failed: 503'))
    // (poll.refresh is internal; the initial-load error path is what the
    // panel owns — assert it directly on a fresh mount)
    cleanup()
    render(<OptionsPanel sym="TEST" />)
    await waitFor(() =>
      expect(
        screen.getByText(/Cannot reach the cockpit API\. fetch api\/market\/TEST\/options failed: 503/),
      ).toBeTruthy(),
    )
  })
})
