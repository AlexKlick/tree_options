import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { ResearchPage } from './ResearchPage'

// Minimal mock for the API wrappers — we test the page renders, fetches the
// catalog, and surfaces ineligible + eligible rows. The compare-spool path
// is not exercised here; it's covered by the integration smoke in the
// /api/research/* route tests.
vi.mock('../lib/api', () => ({
  listResearchCandidates: async () => ({
    candidates: [
      {
        id: 'vix_term-v2',
        family: 'vix_term',
        version: 'v2',
        evidence_kind: 'sealed_campaign',
        registration: 'before_entry_window_end',
        disposition: 'PASS',
        plot_funded_account: true,
        supported_start: '2024-01-02',
        supported_end: '2026-09-25',
        artifact_hashes: {},
        capabilities: ['plot_funded_account'],
        ineligibility_reason: null,
        data_completeness: {},
        warnings: [],
        source_url: 'sealed-round/vix_term',
      },
      {
        id: 'term-gate-v?',
        family: 'term-gate',
        version: 'v?',
        evidence_kind: 'sealed_campaign',
        registration: 'retrospective_backfill',
        disposition: 'WITHDRAWN',
        plot_funded_account: false,
        supported_start: null,
        supported_end: null,
        artifact_hashes: {},
        capabilities: ['view_published_study'],
        ineligibility_reason: 'disposition=WITHDRAWN',
        data_completeness: {},
        warnings: ['research.data_gated'],
        source_url: 'sealed-round/term-gate',
      },
    ],
  }),
  getResearchEvidence: async () => ({}),
  spoolComparison: async () => ({ run_id: 'r1', status: 'queued', spec_hash: 'r1', workspace: '/tmp' }),
}))

describe('ResearchPage (RL-1)', () => {
  it('lists both eligible and ineligible candidates with disposition labels', async () => {
    render(<ResearchPage />)
    await waitFor(() => {
      expect(screen.getByText('vix_term')).toBeTruthy()
      expect(screen.getByText('term-gate')).toBeTruthy()
    })
    // Eligible rows carry the ✓ glyph; ineligible rows carry the — glyph
    expect(screen.getByText('PASS')).toBeTruthy()
    expect(screen.getByText('WITHDRAWN')).toBeTruthy()
  })

  it('shows ineligibility reason inline for non-plot candidates', async () => {
    render(<ResearchPage />)
    await waitFor(() => {
      expect(screen.getByText(/disposition=WITHDRAWN/)).toBeTruthy()
    })
  })

  it('renders a checkbox only for eligible candidates', async () => {
    render(<ResearchPage />)
    await waitFor(() => {
      // Only the eligible candidate (vix_term) has a checkbox; the
      // ineligible one (term-gate) renders a plain row.
      const checkboxes = document.querySelectorAll('input[type="checkbox"]')
      expect(checkboxes.length).toBe(1)
    })
  })
})
