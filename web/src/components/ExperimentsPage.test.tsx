import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ExperimentsPage } from './ExperimentsPage'
import * as api from '../lib/experimentsApi'
import { parseHash, serialize } from '../lib/router'

vi.mock('../lib/experimentsApi', () => ({getExperiments: vi.fn()}))
afterEach(() => { cleanup(); vi.resetAllMocks() })
const row: api.Experiment = {
  id: 'study-1', title: 'Repeated study', source: 'challenge', hypothesis: 'Compare a policy to control.',
  evidence_class: 'historical_simulation', status: 'superseded_accounting', source_time: null,
  freshness: 'archived', freshness_reason: 'Archived result.',
  metrics: [{label: 'Independent sample count', value: null, unit: 'sessions', reason: 'Not recorded.'}],
  limitations: ['No demonstrated edge.'], blockers: [], next_decision: 'Register a new study.',
  artifacts: [], supersedes: null, superseded_by: 'corrected', advanced_link: 'desk/evidence',
}
function data(): api.ExperimentIndex { return {
  published_at: '2026-10-14T20:00:00Z', source_release: 'test',
  sources: [{id: 'challenge', status: 'available', count: 1, reason: null}, {id: 'qsl', status: 'unavailable', count: null, reason: 'source_missing'}],
  experiments: [row], pilot: [],
} }
it('shows unavailable counts, missing sources and supersession without invented returns', async () => {
  vi.mocked(api.getExperiments).mockResolvedValue(data())
  render(<ExperimentsPage />)
  await screen.findByText('Repeated study')
  expect(screen.getByText('Unavailable — Not recorded.')).toBeTruthy()
  expect(screen.getByText(/qsl: source_missing/)).toBeTruthy()
  expect(screen.getByRole('link', {name: 'Read the corrected record'}).getAttribute('href')).toBe('#/experiments/corrected')
  expect(screen.getByText(/equity programs are blocked drafts/)).toBeTruthy()
  fireEvent.change(screen.getByLabelText('Evidence class'), {target: {value: 'broker_paper'}})
  expect(screen.getByText(/not an empty broker portfolio/)).toBeTruthy()
})
it('shows specific publication failure without an empty successful index', async () => {
  vi.mocked(api.getExperiments).mockRejectedValue(new Error('index_stale'))
  render(<ExperimentsPage />)
  expect((await screen.findByRole('alert')).textContent).toContain('index_stale')
  expect(screen.queryByText('Three-program research pilot')).toBeNull()
})
it('deep-links to original details and preserves existing bookmarks', async () => {
  vi.mocked(api.getExperiments).mockResolvedValue(data())
  render(<ExperimentsPage id="study-1" />)
  expect(await screen.findByText('No demonstrated edge.')).toBeTruthy()
  expect(screen.getByRole('link', {name: 'All experiments'})).toBeTruthy()
  expect(parseHash('#/experiments/study-1')).toEqual({view: 'experiments', id: 'study-1'})
  expect(serialize({view: 'experiments', id: 'study-1'})).toBe('#/experiments/study-1')
  expect(parseHash('#/research')).toEqual({view: 'research'})
  expect(parseHash('#/workspace')).toEqual({view: 'workspace'})
})
