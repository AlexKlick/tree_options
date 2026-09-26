/** Research Lab — Scenarios tab (RL-2).
 *
 * Renders the scenario lineage for a chosen parent run, plus a minimal
 * "fork this scenario" form. The component is intentionally thin: it
 * reads the same content-bound result envelope the worker publishes
 * (via GET /api/research/runs/<child>/result), so the SPA and the
 * API agree on the same numbers without a re-computation.
 *
 * Architecture matches the existing ResearchPage: zero new chart
 * library, `usePoll` for the run lifecycle (already exercised by
 * RL1-07 — intervalMs <= 0 disables polling cleanly), `chartGeometry`
 * reused from ResearchNavChart for the diff chart.
 */

import { useEffect, useMemo, useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import {
  getComparisonResult,
  listScenarios,
  spoolScenario,
  getResearchRun,
} from '../lib/api'
import type {
  ComparisonResultWire,
  RunResultResponse,
} from '../lib/types'
import { ResearchNavChart, type NavSeries } from './ResearchNavChart'

const SERIES_COLORS = ['#4f9cf9', '#e8833a', '#3aa88f', '#a25bd6']

function fmtUsd(v: string | null | undefined): string {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (!Number.isFinite(n)) return v
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

interface ScenarioListItem {
  child_run_id: string
  parent_run_id: string
  scenario_kind: string
  scenario_diff_sha256: string
}

function buildChildSeries(result: RunResultResponse, candidateId: string): NavSeries | null {
  // The child's ComparisonResult is published under wire.candidates;
  // look up the same candidate the parent used (RL-2 preserves the
  // parent→child candidate set; the diff is over controls, not over
  // candidate selection).
  const wire = result.result
  if (!wire || typeof wire !== 'object' || !('candidates' in wire)) {
    return null
  }
  const w = wire as ComparisonResultWire
  const summary = (w.candidates ?? []).find(c => c.candidate_id === candidateId)
  if (!summary) return null
  const points: [number, number | null][] = []
  Object.entries(summary.rows_by_date ?? {})
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .forEach(([d, row]) => {
      const nav = row.nav
      const y = nav === null || nav === undefined ? null : Number(nav)
      if (y !== null && !Number.isFinite(y)) return
      points.push([new Date(d).getTime(), y])
    })
  if (points.length === 0) return null
  return { id: candidateId, label: candidateId, points, color: SERIES_COLORS[0] }
}

export function ResearchScenarios(): JSX.Element {
  // The parent field starts EMPTY by design (P2-5): it must hold a
  // completed run's 64-hex run_id, never a candidate id.
  const [parentRunId, setParentRunId] = useState<string>('')
  const [contribution, setContribution] = useState('500')
  const [pickedChild, setPickedChild] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [accessMode, setAccessMode] = useState<'registered' | 'held_out' | 'exploratory'>('exploratory')
  const [kind, setKind] = useState<'contribution_planning' | 'historical_rule_replay'>(
    'contribution_planning',
  )

  const { data: lineage } = usePoll(
    () => listScenarios(parentRunId || undefined),
    30_000,
  )

  const { data: childResult } = usePoll(
    async () =>
      pickedChild ? getComparisonResult(pickedChild) : null,
    5_000,
  )

  const series: NavSeries[] = useMemo(() => {
    if (!pickedChild || !childResult || childResult.status !== 'completed') return []
    const wire = childResult.result
    if (!wire || typeof wire !== 'object' || !('candidates' in wire)) return []
    const w = wire as ComparisonResultWire
    const childSeries: NavSeries[] = []
    const usedColors = new Set<string>()
    for (const summary of w.candidates ?? []) {
      const built = buildChildSeries(
        { ...childResult, result: wire } as RunResultResponse,
        summary.candidate_id,
      )
      if (!built) continue
      const color = SERIES_COLORS.find(c => !usedColors.has(c)) ?? SERIES_COLORS[0]
      usedColors.add(color)
      childSeries.push({ ...built, color })
    }
    return childSeries
  }, [pickedChild, childResult])

  const submit = async () => {
    setSubmitError(null)
    if (!parentRunId.trim()) return
    try {
      const r = await spoolScenario(parentRunId, {
        kind,
        access_mode: accessMode,
        diff: { contribution_per_period: contribution },
      })
      setPickedChild(r.run_id)
      // No manual refresh here: the child-result poll's fetcher is a
      // ref, so calling the old closure now would fetch the PREVIOUS
      // (or null) child; the next 5 s tick picks up the new id.
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : 'submit failed')
    }
  }

  return (
    <div className="research-scenarios" data-testid="research-scenarios-root">
      <header className="research-scenarios__header">
        <h2>Scenarios</h2>
        <p className="research-scenarios__sub">
          A scenario is a fork of a completed comparison: same candidates, same
          window, same calendar — only the controls you change become the diff.
        </p>
      </header>

      <section className="research-scenarios__form">
        <label>
          Parent run id
          <input
            type="text"
            value={parentRunId}
            onChange={e => setParentRunId(e.target.value)}
            placeholder="spec_hash of a completed comparison"
            data-testid="scenarios-parent-input"
          />
        </label>
        <label>
          Kind
          <select
            value={kind}
            onChange={e => setKind(e.target.value as typeof kind)}
            data-testid="scenarios-kind-select"
          >
            <option value="contribution_planning">Contribution planning</option>
            <option value="historical_rule_replay">Historical rule replay</option>
          </select>
        </label>
        <label>
          Contribution (USD / month)
          <input
            type="number"
            min={0}
            value={contribution}
            onChange={e => setContribution(e.target.value)}
            data-testid="scenarios-contribution-input"
          />
        </label>
        <label>
          Access mode
          <select
            value={accessMode}
            onChange={e => setAccessMode(e.target.value as typeof accessMode)}
            data-testid="scenarios-access-select"
          >
            <option value="exploratory">Exploratory</option>
            <option value="held_out">Held-out</option>
            <option value="registered">Registered</option>
          </select>
        </label>
        <button type="button" onClick={submit} data-testid="scenarios-submit">
          Fork scenario
        </button>
        {submitError && (
          <div className="research-scenarios__error" data-testid="scenarios-submit-error">
            {submitError}
          </div>
        )}
      </section>

      <section className="research-scenarios__lineage">
        <h3>Children of {parentRunId || '(none)'}</h3>
        {(!lineage || lineage.scenarios.length === 0) ? (
          <div className="research-scenarios__empty">No scenarios yet.</div>
        ) : (
          <ul data-testid="scenarios-list">
            {(lineage.scenarios as ScenarioListItem[]).map(s => (
              <li key={s.child_run_id}>
                <button
                  type="button"
                  onClick={() => setPickedChild(s.child_run_id)}
                  data-testid={`scenarios-pick-${s.child_run_id}`}
                >
                  <code>{s.scenario_kind}</code>
                  <span> diff={s.scenario_diff_sha256.slice(0, 12)}…</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* A refused scenario is an HONEST BLOCKER, not an empty chart:
          the refusal wire ({refusal, message}) has no candidates, so
          gating the chart/totals on their presence alone rendered a
          silently blank workspace. Failed runs surface their error
          the same way. */}
      {pickedChild && childResult && childResult.status === 'failed' && (
        <section className="research-scenarios__refusal" data-testid="scenarios-failure">
          <h3>Run failed</h3>
          <p className="error">{childResult.error ?? 'the worker recorded an error'}</p>
        </section>
      )}
      {pickedChild && childResult && childResult.status === 'completed' && (
        <ScenarioOutcome childResult={childResult} series={series} />
      )}

      {pickedChild && (
        <ChildLifecycleProbe childRunId={pickedChild} />
      )}
    </div>
  )
}

/** Renders a completed scenario run's outcome: a typed refusal is a
 * first-class result (the worker records it content-bound), shown as
 * the honest blocker with its code + message; a computed result
 * renders the diff chart and the totals table. */
function ScenarioOutcome({
  childResult,
  series,
}: {
  childResult: RunResultResponse
  series: NavSeries[]
}): JSX.Element | null {
  const wire = childResult.result
  const refused =
    wire !== null &&
    typeof wire === 'object' &&
    'refusal' in wire &&
    (wire as { refusal?: string | null }).refusal
  if (refused) {
    const w = wire as { refusal: string; message?: string; scenario_kind?: string }
    return (
      <section className="research-scenarios__refusal" data-testid="scenarios-refusal">
        <h3>Scenario refused: <code>{w.refusal}</code></h3>
        <p className="muted">{w.message ?? 'no refusal message recorded'}</p>
        <p className="muted">
          A refusal is a recorded outcome, not an error — the worker
          published it content-bound. Fix the diff or the parent, then
          fork again.
        </p>
      </section>
    )
  }
  if (!wire || typeof wire !== 'object' || !('candidates' in wire)) {
    return (
      <section className="research-scenarios__refusal" data-testid="scenarios-empty-result">
        <p className="muted">
          The recorded result carries no candidate series for this fork.
        </p>
      </section>
    )
  }
  const w = wire as ComparisonResultWire
  return (
    <>
      <section className="research-scenarios__chart">
        <h3>Diff chart</h3>
        <ResearchNavChart series={series} ariaLabel="scenario diff chart" />
      </section>
      <section className="research-scenarios__totals">
        <h3>Totals</h3>
        <table data-testid="scenarios-totals">
          <thead>
            <tr><th>Candidate</th><th>Final NAV</th><th>Total fees</th></tr>
          </thead>
          <tbody>
            {(w.candidates ?? []).map(s => (
              <tr key={s.candidate_id} data-testid={`scenarios-total-${s.candidate_id}`}>
                <td>{s.candidate_id}</td>
                <td>{fmtUsd(s.final_ending_value)}</td>
                <td>{fmtUsd(s.fees_paid_total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  )
}

/** Tiny inline lifecycle probe so the SPA confirms a child run
 * actually exists in the runstate store after submit. The probe
 * reads /api/research/runs/<id> on every child change and surfaces
 * the status the store currently records. */
function ChildLifecycleProbe({ childRunId }: { childRunId: string }): JSX.Element {
  const [lifecycle, setLifecycle] = useState<string>('idle')
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const r = await getResearchRun(childRunId)
      if (!cancelled) setLifecycle(typeof r === 'object' && r && 'status' in r ? String((r as { status: string }).status) : 'unknown')
    })()
    return () => { cancelled = true }
  }, [childRunId])
  return (
    <div className="research-scenarios__lifecycle" data-testid="scenarios-lifecycle">
      <span>Last fetched run status: </span><code>{lifecycle}</code>
    </div>
  )
}
