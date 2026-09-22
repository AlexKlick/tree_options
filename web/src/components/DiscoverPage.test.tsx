import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { CandidateRow, DiscoveryLatest } from '../lib/types'
import { CandidateTable } from './CandidateTable'

function candidate(over: Partial<CandidateRow> = {}): CandidateRow {
  return {
    underlying: 'NVDA',
    expiry: '20261016',
    dte: 24,
    short_strike: 150,
    long_strike: 185,
    width: 35,
    debit_mid: 0.21,
    debit_bid: 0.19,
    debit_ask: 0.23,
    short_mid: 0.43,
    long_mid: 0.64,
    short_delta: null,
    long_delta: null,
    short_spread_frac: 0.14,
    long_spread_frac: 0.1,
    yield_ratio: 165.7,
    max_profit: 16520,
    max_loss: 735,
    target_mode_used: 'premium',
    rank: 1,
    accepted: true,
    rules: [{ rule: 'dte', status: 'PASS', detail: 'dte 24' }],
    reasons: [],
    ...over,
  }
}

const latest: DiscoveryLatest = {
  run_id: '20260922T210000.000000Z',
  generated_at: '2026-09-22T17:00:00-04:00',
  age_seconds: 30,
  mode: 'manual',
  request_id: null,
  git_sha: 'test',
  config_hash: 'x',
  effective_target_modes: ['premium'],
  data_quality: {
    underlyings_requested: 5,
    underlyings_scanned: 5,
    chains_available: true,
    greeks_available: false,
    expiries_scanned: 5,
    rows_quoted: 100,
    rows_unquoted: 4,
    notes: ['delayed data', 'auto degraded to premium: no greeks observed'],
  },
  candidates: [candidate()],
  rejected: [
    candidate({
      underlying: 'QQQ',
      accepted: false,
      rank: 0,
      debit_mid: null,
      yield_ratio: null,
      max_profit: null,
      max_loss: null,
      reasons: ['no market on short leg'],
    }),
  ],
}

describe('CandidateTable', () => {
  it('formats debit, money and yield ratio', () => {
    render(<CandidateTable rows={latest.candidates} />)
    expect(screen.getByText(/NVDA/)).toBeTruthy()
    expect(screen.getByText('185/150')).toBeTruthy()
    expect(screen.getByText('$0.21')).toBeTruthy()
    expect(screen.getByText('+$16,520')).toBeTruthy()
    expect(screen.getByText('165.7:1')).toBeTruthy()
  })

  it('rejected rows show no rank and collapse the audit', () => {
    render(<CandidateTable rows={latest.rejected} />)
    const rankCell = screen.getByText('—', { selector: 'td.num.muted' })
    expect(rankCell).toBeTruthy() // no rank for rejected
    expect(screen.getByText('why rejected')).toBeTruthy()
  })
})
