/** Research Lab — the comparison workspace (RL-1 completion).
 *
 * Chart-first: pick strategies + benchmark + capital + window, run the
 * comparison, and inspect the RESULT — portfolio value in dollars with
 * contributions visible, drawdown, difference from the benchmark, a
 * numeric table, and point-linked evidence. The catalog stays available
 * for finding candidates and reading honest blockers.
 *
 * Synthetic candidates (the machinery-validation slice) are badged
 * everywhere they appear; their numbers are invented and never
 * investment evidence.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import {
  getComparisonResult,
  getResearchEvidence,
  getResearchRun,
  listResearchCandidates,
  spoolComparison,
} from '../lib/api'
import type {
  CandidateResultSummary,
  ComparisonResultWire,
  ResearchCandidate,
  RunResultResponse,
} from '../lib/types'
import { ResearchNavChart, type NavSeries } from './ResearchNavChart'
import { ResearchScenarios } from './ResearchScenarios'

const SERIES_COLORS = ['#4f9cf9', '#e8833a', '#3aa88f', '#a25bd6']

function isSynthetic(c: ResearchCandidate): boolean {
  return c.evidence_kind === 'synthetic_backtest'
}

function fmtUsd(v: string | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return v
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

export function ResearchPage(): JSX.Element {
  const [tab, setTab] = useState<'comparison' | 'scenarios'>('comparison')

  return (
    <div className="research-page">
      <h1>Research Lab</h1>
      <p className="muted">
        Compare investments on a common declared basis: same calendar,
        same capital, same costs. Scenarios fork a parent comparison to
        quantify how a single control change moves the result.
      </p>
      <nav className="research-page__tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'comparison'}
          onClick={() => setTab('comparison')}
          data-testid="research-tab-comparison"
        >
          Comparison
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'scenarios'}
          onClick={() => setTab('scenarios')}
          data-testid="research-tab-scenarios"
        >
          Scenarios
        </button>
      </nav>
      {tab === 'comparison' ? <ComparisonWorkspace /> : <ResearchScenarios />}
    </div>
  )
}

/** The RL-1 comparison workspace — lifted into its own component so
 * the tab strip above can mount it without re-creating state in the
 * Scenarios tab. The behavior is unchanged from the RL-1 landing. */
function ComparisonWorkspace(): JSX.Element {
  const { data: catalog, error: catalogError } = usePoll(
    () => listResearchCandidates(),
    30_000,
  )

  const candidates = useMemo(() => catalog?.candidates ?? [], [catalog])
  const eligible = useMemo(
    () => candidates.filter((c: ResearchCandidate) => c.plot_funded_account),
    [candidates],
  )

  const [picked, setPicked] = useState<string[]>([])
  const [benchmarkId, setBenchmarkId] = useState<string>('')
  const [startingCapital, setStartingCapital] = useState('10000')
  const [windowStart, setWindowStart] = useState('')
  const [windowEnd, setWindowEnd] = useState('')
  const [contribution, setContribution] = useState('0')
  const [runId, setRunId] = useState<string | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [result, setResult] = useState<ComparisonResultWire | null>(null)
  const [evidenceFor, setEvidenceFor] = useState<{ cid: string; session: string } | null>(null)
  const resultFetchedFor = useRef<string | null>(null)

  useEffect(() => {
    if (picked.length === 0 && eligible.length >= 2) {
      setPicked(eligible.slice(0, 2).map((c: ResearchCandidate) => c.id))
    }
    if (!benchmarkId && eligible.length >= 3) {
      const bench = eligible.find((c: ResearchCandidate) => c.family.includes('benchmark'))
      if (bench) setBenchmarkId(bench.id)
    }
  }, [eligible, picked.length, benchmarkId])

  // Run lifecycle: bounded polling while the run is live; the result is
  // fetched exactly once per run when the status turns completed.
  const { data: run } = usePoll(
    () => (runId ? getResearchRun(runId) : Promise.resolve(null)),
    runId ? 2_500 : 0,
  )
  const status = run?.status
  useEffect(() => {
    if (!runId || status !== 'completed') return
    if (resultFetchedFor.current === runId) return
    resultFetchedFor.current = runId
    void getComparisonResult(runId).then((envelope: RunResultResponse) => {
      setResult(envelope.result)
      if (envelope.error) setRunError(envelope.error)
    }).catch((err: unknown) => {
      setRunError(err instanceof Error ? err.message : String(err))
    })
  }, [runId, status])

  async function onRunComparison(): Promise<void> {
    setRunError(null)
    setResult(null)
    try {
      const queued = await spoolComparison({
        candidate_ids: picked,
        starting_capital: startingCapital,
        common_start: windowStart || null,
        common_end: windowEnd || null,
        cashflow_timing: 'beginning_of_period',
        contribution_per_period: contribution || '0',
        cost_model_kind: 'five_bp_fixed',
        benchmark_candidate_id: benchmarkId || null,
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
      })
      resultFetchedFor.current = null
      setRunId(queued.run_id)
    } catch (err) {
      setRunError(err instanceof Error ? err.message : String(err))
    }
  }

  const plotted = useMemo(() => result?.candidates.filter((s) => !s.rejection_reason) ?? [],
    [result])
  const rejected = useMemo(() => result?.candidates.filter((s) => s.rejection_reason) ?? [],
    [result])
  const anySynthetic = plotted.some((s) => isSynthetic(s.candidate))
    || candidates.some((c) => c.id === benchmarkId && isSynthetic(c))

  const navSeries = useMemo<NavSeries[]>(() => {
    const series: { id: string; label: string; summary: CandidateResultSummary }[] =
      plotted.map((s) => ({ id: s.candidate_id, label: s.candidate.family, summary: s }))
    return series.map((entry, i) => ({
      id: entry.id,
      label: entry.label,
      color: SERIES_COLORS[i % SERIES_COLORS.length],
      points: Object.entries(entry.summary.rows_by_date)
        .sort(([a], [b]) => (a < b ? -1 : 1))
        .map(([dateIso, row]): [number, number | null] => [
          Date.parse(`${dateIso}T21:00:00Z`),
          row.nav === null ? null : Number(row.nav),
        ]),
    }))
  }, [plotted])

  return (
    <div className="comparison-workspace">
      {catalogError && <p className="error">catalog error: {catalogError}</p>}

      <section className="controls" aria-label="comparison controls">
        <h2>Comparison</h2>
        <ul className="pick-list">
          {eligible.map((c: ResearchCandidate) => (
            <li key={c.id}>
              <label>
                <input
                  type="checkbox"
                  checked={picked.includes(c.id)}
                  onChange={(e) => {
                    setPicked((cur) => (e.target.checked
                      ? [...cur, c.id].slice(0, 4)
                      : cur.filter((x) => x !== c.id)))
                  }}
                />
                {' '}{c.family}
                {isSynthetic(c) && <span className="synthetic-badge">SYNTHETIC</span>}
              </label>
              {c.id === benchmarkId ? null : (
                <label className="bench-pick">
                  <input
                    type="radio"
                    name="benchmark"
                    checked={false}
                    onChange={() => setBenchmarkId(c.id)}
                  />{' '}benchmark
                </label>
              )}
            </li>
          ))}
        </ul>
        <label className="bench-current">
          Benchmark:{' '}
          <select value={benchmarkId} onChange={(e) => setBenchmarkId(e.target.value)}>
            <option value="">none</option>
            {eligible.map((c: ResearchCandidate) => (
              <option key={c.id} value={c.id}>{c.family}</option>
            ))}
          </select>
        </label>
        <label>
          Starting capital (USD):{' '}
          <input type="text" value={startingCapital} onChange={(e) => setStartingCapital(e.target.value)} />
        </label>
        <label>
          From <input type="date" value={windowStart} onChange={(e) => setWindowStart(e.target.value)} />
        </label>
        <label>
          to <input type="date" value={windowEnd} onChange={(e) => setWindowEnd(e.target.value)} />
        </label>
        <label>
          Monthly contribution (USD):{' '}
          <input type="text" value={contribution} onChange={(e) => setContribution(e.target.value)} />
        </label>
        <button
          type="button"
          onClick={onRunComparison}
          disabled={picked.length === 0 || !windowStart || !windowEnd}
        >
          Run comparison
        </button>
        {anySynthetic && (
          <p className="muted">
            <span className="synthetic-badge">SYNTHETIC</span>
            {' '}fixture data — machinery validation only, not investment evidence.
          </p>
        )}
      </section>

      <section className="run-status" aria-live="polite">
        {runId && (
          <p>
            run <code>{runId.slice(0, 12)}…</code>:{' '}
            {status === 'completed' ? 'completed' : (status ?? 'queued')}
          </p>
        )}
        {runError && <p className="error">{runError}</p>}
      </section>

      {result && (
        <>
          {result.rejection && (
            <p className="error">comparison refused: {result.rejection}</p>
          )}
          {rejected.map((s) => (
            <p key={s.candidate_id} className="muted">
              {s.candidate.family}: {s.rejection_reason}
            </p>
          ))}

          {plotted.length > 0 && (
            <>
              <section aria-label="summaries">
                <h2>Summary</h2>
                <div className="summary-tiles">
                  {plotted.map((s) => {
                    const dates = Object.keys(s.rows_by_date).sort()
                    const first = s.rows_by_date[dates[0]]
                    const last = s.rows_by_date[dates[dates.length - 1]]
                    const gaps = Object.values(s.rows_by_date)
                      .filter((r) => r.nav === null).length
                    return (
                      <div key={s.candidate_id} className="summary-tile">
                        <div className="tile-label">
                          {s.candidate.family}
                          {isSynthetic(s.candidate) && <span className="synthetic-badge">SYNTHETIC</span>}
                        </div>
                        <div className="tile-value">{fmtUsd(s.final_ending_value)}</div>
                        <div>contributed {fmtUsd(last?.contributions_cum)}</div>
                        <div>gain {fmtUsd(last?.investment_gain)}</div>
                        <div>
                          coverage {dates.length - gaps}/{dates.length} sessions
                          {first?.fees_cum && ` · fees ${fmtUsd(s.fees_paid_total)}`}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </section>

              <section aria-label="portfolio value chart">
                <h2>Portfolio value (USD)</h2>
                <ResearchNavChart series={navSeries} ariaLabel="Portfolio value by strategy" />
              </section>

              <section aria-label="numeric table">
                <h2>Values by session</h2>
                <table>
                  <thead>
                    <tr>
                      <th>session</th>
                      {plotted.map((s) => <th key={s.candidate_id}>{s.candidate.family}</th>)}
                      <th>evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {Object.keys(plotted[0]?.rows_by_date ?? {}).sort().map((dateIso) => (
                      <tr key={dateIso}>
                        <td>{dateIso}</td>
                        {plotted.map((s) => (
                          <td key={s.candidate_id} className="num">
                            {s.rows_by_date[dateIso]?.nav === null
                              ? 'gap'
                              : fmtUsd(s.rows_by_date[dateIso]?.nav)}
                          </td>
                        ))}
                        <td>
                          <button
                            type="button"
                            onClick={() => setEvidenceFor({ cid: plotted[0].candidate_id, session: dateIso })}
                          >
                            inspect
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            </>
          )}
        </>
      )}

      <section className="catalog">
        <h2>Candidates ({candidates.length})</h2>
        <table>
          <thead>
            <tr>
              <th>family</th>
              <th>disposition</th>
              <th>evidence</th>
              <th>funded history</th>
              <th>supported</th>
              <th>blocker</th>
              <th>drawer</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((c: ResearchCandidate) => (
              <tr key={c.id} className={c.plot_funded_account ? 'eligible' : 'ineligible'}>
                <td>
                  {c.family}
                  {isSynthetic(c) && <span className="synthetic-badge">SYNTHETIC</span>}
                </td>
                <td>{c.disposition}</td>
                <td>{c.evidence_kind}</td>
                <td>{c.funded_history}</td>
                <td>{c.supported_start ?? '?'} → {c.supported_end ?? '?'}</td>
                <td>
                  {(c.ineligibility_reason || c.funded_history_reason) && (
                    <span className="muted ineligibility">
                      {c.ineligibility_reason ?? c.funded_history_reason}
                    </span>
                  )}
                </td>
                <td>
                  <button type="button" onClick={() => setEvidenceFor({ cid: c.id, session: '' })}>
                    open
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {evidenceFor && (
        <EvidenceDrawer
          key={`${evidenceFor.cid}:${evidenceFor.session}`}
          candidateId={evidenceFor.cid}
          session={evidenceFor.session || null}
          onClose={() => setEvidenceFor(null)}
        />
      )}
    </div>
  )
}

function EvidenceDrawer(props: {
  candidateId: string
  session: string | null
  onClose: () => void
}): JSX.Element {
  const { candidateId, session, onClose } = props
  // 0 = polling disabled: one-shot load per mount (see usePoll).
  const { data: env, error } = usePoll(
    () => getResearchEvidence(candidateId, session ? { session } : {}),
    0,
  )
  return (
    <aside className="evidence-drawer" role="dialog" aria-label="evidence drawer">
      <button type="button" onClick={onClose}>close</button>
      <h2>Evidence: {candidateId}{session ? ` · ${session}` : ''}</h2>
      {error && <p className="error">{error}</p>}
      {env && (
        <>
          <p><strong>Hypothesis:</strong> {env.hypothesis}</p>
          <p><strong>Estimand:</strong> {env.estimand}</p>
          <p><strong>Registration:</strong> {env.registered_or_exploratory}</p>
          <p><strong>Reproduction:</strong> <code>{env.reproduction_command}</code></p>
          {env.source_artifacts.length > 0 && (
            <ul>
              {env.source_artifacts.map((a) => (
                <li key={a.path}><code>{a.path}</code> · sha {a.sha256.slice(0, 12)}…</li>
              ))}
            </ul>
          )}
          {env.warnings.length > 0 && (
            <p className="warnings">Warnings: {env.warnings.join('; ')}</p>
          )}
        </>
      )}
    </aside>
  )
}
