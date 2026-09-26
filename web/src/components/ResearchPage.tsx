/** RL-1: catalog + comparison workspace.
 *
 * Renders the Research Lab's seeded catalog (per ``listResearchCandidates``)
 * with per-candidate disposition + eligibility, plus a minimal "Run a
 * comparison" form that spools a ResearchRun via ``spoolComparison`` and
 * reads the result via ``getComparisonResult``.
 *
 * The full charting, evidence-drawer, and what-if surfaces live in
 * later milestones (RL-2, RL-3). RL-1 keeps the page read-only on the
 * result and renders a tabular funding summary instead of a wallet
 * curve.
 */

import { useEffect, useMemo, useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import {
  getResearchEvidence,
  listResearchCandidates,
  spoolComparison,
} from '../lib/api'
import type {
  ComparisonSpec,
  ResearchCandidate,
} from '../lib/types'

export function ResearchPage(): JSX.Element {
  const { data: catalog, error: catalogError } = usePoll(
    () => listResearchCandidates(),
    30_000,
  )

  const candidates = catalog?.candidates ?? []
  const eligible = useMemo(
    () => candidates.filter((c: ResearchCandidate) => c.plot_funded_account),
    [candidates],
  )

  const [picked, setPicked] = useState<string[]>([])
  const [startingCapital, setStartingCapital] = useState('10000')
  const [runId, setRunId] = useState<string | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [evidenceFor, setEvidenceFor] = useState<string | null>(null)

  useEffect(() => {
    if (picked.length === 0 && eligible.length >= 2) {
      setPicked(eligible.slice(0, 2).map((c: ResearchCandidate) => c.id))
    }
  }, [eligible, picked.length])

  async function onRunComparison(): Promise<void> {
    setRunError(null)
    try {
      const spec: ComparisonSpec = {
        candidate_ids: picked,
        starting_capital: startingCapital,
        common_start: null,
        common_end: null,
        cashflow_timing: 'beginning_of_period',
        contribution_per_period: '0',
        cost_model_kind: 'five_bp_fixed',
        benchmark_candidate_id: null,
        currency: 'USD',
        price_basis: 'nominal_pretax',
        idle_cash_policy: 'cash_yields_zero',
        rebalancing: 'none',
        position_sizing: 'integer',
        collateral: 'none',
        borrowing: 'none',
        knowledge_cutoff: null,
        proposed_by: 'operator',
        notes: '',
      }
      const queued = await spoolComparison(spec)
      setRunId(queued.run_id)
    } catch (err) {
      setRunError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="research-page">
      <h1>Research Lab</h1>
      <p className="muted">
        RL-1: catalog of sealed-round candidates. RL-2 (scenario branching)
        and RL-3 (calibrated outlook) return 410 Gone until shipped.
      </p>

      {catalogError && (
        <p className="error">catalog error: {catalogError}</p>
      )}

      <section className="catalog">
        <h2>Candidates ({candidates.length})</h2>
        <table>
          <thead>
            <tr>
              <th>pick</th>
              <th>family</th>
              <th>disposition</th>
              <th>evidence</th>
              <th>supported</th>
              <th>plot</th>
              <th>evidence drawer</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((c: ResearchCandidate) => (
              <tr key={c.id} className={c.plot_funded_account ? 'eligible' : 'ineligible'}>
                <td>
                  {c.plot_funded_account ? (
                    <input
                      type="checkbox"
                      checked={picked.includes(c.id)}
                      onChange={(e) => {
                        setPicked((cur) => (e.target.checked
                          ? [...cur, c.id]
                          : cur.filter((x) => x !== c.id)))
                      }}
                    />
                  ) : null}
                </td>
                <td>{c.family}</td>
                <td>{c.disposition}</td>
                <td>{c.evidence_kind}</td>
                <td>
                  {c.supported_start ?? '?'} → {c.supported_end ?? '?'}
                </td>
                <td>{c.plot_funded_account ? '✓' : '—'}</td>
                <td>
                  {c.ineligibility_reason && (
                    <span className="muted ineligibility">
                      {c.ineligibility_reason}
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => setEvidenceFor(c.id)}
                  >
                    open
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="run">
        <h2>Run a comparison</h2>
        <label>
          Starting capital (USD):
          <input
            type="text"
            value={startingCapital}
            onChange={(e) => setStartingCapital(e.target.value)}
          />
        </label>
        <p>Picked: {picked.length ? picked.join(', ') : '—'}</p>
        <button type="button" onClick={onRunComparison} disabled={picked.length < 2}>
          Spool run
        </button>
        {runError && <p className="error">spool error: {runError}</p>}
        {runId && <p>queued: <code>{runId}</code></p>}
      </section>

      {evidenceFor && (
        <EvidenceDrawer candidateId={evidenceFor}
                        onClose={() => setEvidenceFor(null)} />
      )}
    </div>
  )
}

function EvidenceDrawer(props: { candidateId: string; onClose: () => void }): JSX.Element {
  const { candidateId, onClose } = props
  const { data: env, error } = usePoll(
    () => getResearchEvidence(candidateId),
    0,
  )
  return (
    <aside className="evidence-drawer" role="dialog">
      <button type="button" onClick={onClose}>close</button>
      <h2>Evidence: {candidateId}</h2>
      {error && <p className="error">{error}</p>}
      {env && (
        <>
          <p><strong>Hypothesis:</strong> {env.hypothesis}</p>
          <p><strong>Estimand:</strong> {env.estimand}</p>
          <p><strong>Reproduction:</strong> <code>{env.reproduction_command}</code></p>
          {env.warnings.length > 0 && (
            <p className="warnings">Warnings: {env.warnings.join('; ')}</p>
          )}
        </>
      )}
    </aside>
  )
}
