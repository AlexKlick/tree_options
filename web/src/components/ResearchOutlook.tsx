/** Research Lab — Outlook tab (RL-3): evaluation receipts for quantile
 * forecasts, plus the latest-origin forward fan.
 *
 * Honesty rules baked into this component:
 *   * a listed-but-disabled horizon (63/126) renders its exact
 *     status_copy and is NOT selectable — the UI hides nothing the
 *     backend would refuse pre-write;
 *   * NO FAN WITHOUT A RECEIPT: the fan chart renders only inside a
 *     completed run's evaluation receipt, never from metadata alone;
 *   * coverage is always quoted with its n (Wilson + bootstrap),
 *     excluded and failed origins are visible, the baseline row is
 *     labeled, and calibration is shown exactly as recorded —
 *     'not claimed';
 *   * the empty state says "run evaluation" — no copy anywhere says
 *     calibrate or calibrated;
 *   * receipt rendering is keyed by run id and guarded by it, so a
 *     slow response for a previous run can never render under the
 *     current selection.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import {
  getForecastMetadata,
  getForecastResult,
  getForecastRun,
  spoolForecast,
} from '../lib/api'
import {
  coverageLine,
  fmtLevel,
  hasReceipt,
  horizonEntry,
  isDegradedMetrics,
  isForecastRefusal,
  skillLine,
} from '../lib/forecast'
import type {
  ForecastHorizonStatus,
  ForecastRunResultResponse,
  ForecastSpec,
} from '../lib/types'
import { ForecastFanChart } from './ForecastFanChart'

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export function ResearchOutlook(): JSX.Element {
  const { data: metadata, error: metadataError } = usePoll(
    () => getForecastMetadata(),
    30_000,
  )

  const [source, setSource] = useState<string | null>(null)
  const [horizon, setHorizon] = useState<number | null>(null)
  const [evalStart, setEvalStart] = useState('')
  const [evalEnd, setEvalEnd] = useState('')
  const [runId, setRunId] = useState<string | null>(null)
  const [runError, setRunError] = useState<string | null>(null)
  const [result, setResult] = useState<ForecastRunResultResponse | null>(null)
  const resultFetchedFor = useRef<string | null>(null)

  // Defaults follow the registry: first source, first ENABLED horizon.
  useEffect(() => {
    if (!metadata || source !== null) return
    setSource(metadata.sources[0]?.source ?? null)
  }, [metadata, source])
  useEffect(() => {
    if (!metadata || source === null || horizon !== null) return
    const enabled = metadata.sources
      .find((s) => s.source === source)
      ?.horizons.find((h) => h.enabled)
    setHorizon(enabled?.horizon ?? null)
  }, [metadata, source, horizon])

  const sourceStatus = useMemo(
    () => metadata?.sources.find((s) => s.source === source) ?? null,
    [metadata, source],
  )
  const horizonStatus: ForecastHorizonStatus | undefined = useMemo(
    () => (metadata && source !== null && horizon !== null
      ? horizonEntry(metadata, source, horizon)
      : undefined),
    [metadata, source, horizon],
  )

  /** Moving the lane selection invalidates whatever run was loaded —
   * the operator is looking at a different source/horizon now, and no
   * receipt from the old lane may present itself as this one's. */
  function onPickSource(next: string): void {
    if (next === source) return
    setSource(next)
    setHorizon(null)
    setRunId(null)
    setResult(null)
    setRunError(null)
    resultFetchedFor.current = null
  }
  function onPickHorizon(next: number): void {
    if (next === horizon) return
    setHorizon(next)
    setRunId(null)
    setResult(null)
    setRunError(null)
    resultFetchedFor.current = null
  }

  // Run lifecycle: bounded polling while live; the receipt is fetched
  // exactly once per run, and a superseded run's late response is
  // dropped (run-keying — the receipt renders under its own id only).
  const { data: run } = usePoll(
    () => (runId ? getForecastRun(runId) : Promise.resolve(null)),
    runId ? 2_500 : 0,
  )
  const status = run?.status
  useEffect(() => {
    if (!runId || status !== 'completed') return
    if (resultFetchedFor.current === runId) return
    resultFetchedFor.current = runId
    let superseded = false
    void getForecastResult(runId).then((envelope) => {
      if (superseded || resultFetchedFor.current !== runId) return
      setResult(envelope)
    }).catch((err: unknown) => {
      if (superseded || resultFetchedFor.current !== runId) return
      setRunError(errText(err))
    })
    return () => { superseded = true }
  }, [runId, status])

  async function onRunEvaluation(): Promise<void> {
    if (!source || horizon === null || !evalStart) return
    setRunError(null)
    setResult(null)
    const spec: ForecastSpec = {
      source,
      horizon,
      evaluation_start: evalStart,
      evaluation_end: evalEnd || null,
      proposed_by: 'operator',
      notes: '',
    }
    try {
      const queued = await spoolForecast(spec)
      resultFetchedFor.current = null
      setRunId(queued.run_id)
    } catch (err) {
      setRunError(errText(err))
    }
  }

  const receiptReady = hasReceipt(result) && result.run_id === runId
  const refusalReady = result !== null && result.run_id === runId
    && result.status === 'completed' && isForecastRefusal(result.result)
  const noReceiptYet = horizonStatus !== undefined && horizonStatus.enabled
    && horizonStatus.latest_receipt_run_id === null && !runId
    && !receiptReady && !refusalReady

  const wilsonNote = useMemo(() => {
    if (!receiptReady || !result) return null
    const w = result.result
    if (!w || w.schema !== 'research-forecast-result/1') return null
    const m = w.models.find((mm) => !isDegradedMetrics(mm.metrics))
    return m && !isDegradedMetrics(m.metrics)
      ? m.metrics.coverage_90.wilson_note
      : null
  }, [receiptReady, result])

  return (
    <div className="research-outlook" data-testid="research-outlook-root">
      <header>
        <h2>Outlook</h2>
        <p className="muted">
          Quantile forecasts of the index level h sessions ahead, evaluated on
          a rolling monthly-origin grid. Every number below comes from a
          recorded, content-bound evaluation receipt — nothing is recomputed
          in the browser.
        </p>
      </header>

      {metadataError && <p className="error">metadata error: {metadataError}</p>}
      {!metadata && !metadataError && <p className="muted">loading registry…</p>}

      {metadata && (
        <>
          <section className="controls" aria-label="outlook controls">
            <label>
              Source
              <select
                value={source ?? ''}
                onChange={(e) => onPickSource(e.target.value)}
                data-testid="outlook-source-select"
              >
                {metadata.sources.map((s) => (
                  <option key={s.source} value={s.source}>{s.label}</option>
                ))}
              </select>
            </label>
            <label>
              Horizon (sessions)
              <select
                value={horizon === null ? '' : String(horizon)}
                onChange={(e) => onPickHorizon(Number(e.target.value))}
                data-testid="outlook-horizon-select"
              >
                {(sourceStatus?.horizons ?? []).map((h) => h.enabled
                  ? (
                    <option key={h.horizon} value={h.horizon}>
                      {h.horizon}
                    </option>
                  )
                  : (
                    <option key={h.horizon} value={h.horizon} disabled>
                      {h.horizon} — {h.status_copy}
                    </option>
                  ))}
              </select>
            </label>
            <label>
              Evaluation from
              <input
                type="date"
                value={evalStart}
                onChange={(e) => setEvalStart(e.target.value)}
                data-testid="outlook-eval-start"
              />
            </label>
            <label>
              to (optional)
              <input
                type="date"
                value={evalEnd}
                onChange={(e) => setEvalEnd(e.target.value)}
                data-testid="outlook-eval-end"
              />
            </label>
            <button
              type="button"
              onClick={onRunEvaluation}
              disabled={!source || horizon === null || !evalStart
                || horizonStatus?.enabled !== true}
              data-testid="outlook-run"
            >
              Run evaluation
            </button>
            {runError && <p className="error">{runError}</p>}
          </section>

          <section aria-label="interval semantics" data-testid="outlook-semantics">
            <h3>Interval semantics</h3>
            <p className="muted">{metadata.interval_semantics}</p>
            <p className="muted">
              Origin floor {metadata.origin_floor} evaluated origins before a
              receipt may publish; skill and Diebold-Mariano need
              {' '}{metadata.paired_floor} paired origins. Calibration is
              never claimed on this surface.
            </p>
          </section>

          {horizonStatus?.enabled === true && (
            <section className="run-status" aria-live="polite" data-testid="outlook-freshness">
              {horizonStatus.latest_receipt_run_id === null
                ? <p className="muted">No recorded receipt for this horizon.</p>
                : (
                  <p className={horizonStatus.fresh ? 'muted' : 'warnings'}>
                    receipt <code>{horizonStatus.latest_receipt_run_id.slice(0, 12)}…</code>:{' '}
                    {horizonStatus.fresh === null ? 'freshness unknown'
                      : horizonStatus.fresh
                        ? 'fresh (data, engine, and both calendars match the current world)'
                        : 'STALE — the data, engine, or a calendar moved after this receipt; run evaluation again under the current world'}
                  </p>
                )}
              {horizonStatus.last_attempt_refused && (
                <p className="warnings">
                  last attempt refused:{' '}
                  <code>{horizonStatus.last_attempt_refused.code}</code>
                  {horizonStatus.last_attempt_refused.n_evaluated !== null
                    && ` (${horizonStatus.last_attempt_refused.n_evaluated} origins evaluated)`}
                </p>
              )}
            </section>
          )}

          {runId && (
            <section className="run-status" aria-live="polite" data-testid="outlook-status">
              <p>
                run <code>{runId.slice(0, 12)}…</code>:{' '}
                {status === 'completed' ? 'completed' : (status ?? 'queued')}
              </p>
            </section>
          )}

          {noReceiptYet && (
            <section className="muted" data-testid="outlook-empty">
              <p>
                No evaluation receipt for this horizon yet — run evaluation to
                publish one. Nothing is shown without a receipt: no fan, no
                coverage, no skill.
              </p>
            </section>
          )}

          {refusalReady && result !== null && (
            <OutlookRefusalBlock
              key={result.run_id}
              response={result}
            />
          )}

          {receiptReady && result !== null && (
            <OutlookReceiptBlock
              key={result.run_id}
              response={result}
              quantileGrid={metadata.quantile_grid}
              wilsonNote={wilsonNote}
            />
          )}

          {result !== null && result.run_id === runId && result.status === 'failed' && (
            <section className="error" data-testid="outlook-failure">
              <h3>Run failed</h3>
              <p>{result.error ?? 'the worker recorded an error'}</p>
            </section>
          )}
        </>
      )}
    </div>
  )
}

/** The honest blocker: a typed refusal is a recorded outcome with the
 * evidence retained — code, message, tally when present, and the
 * ledger rows the refusal kept. Never a fan. */
function OutlookRefusalBlock({
  response,
}: {
  response: ForecastRunResultResponse
}): JSX.Element {
  const wire = response.result
  if (!wire || typeof wire.refusal !== 'string') return <></>
  return (
    <section className="research-outlook__refusal" data-testid="outlook-refusal">
      <h3>Evaluation refused: <code>{wire.refusal}</code></h3>
      <p className="muted">{wire.message ?? 'no refusal message recorded'}</p>
      {wire.origins && (
        <p>
          origins: {wire.origins.evaluated} evaluated /{' '}
          {wire.origins.excluded} excluded of {wire.origins.total}
        </p>
      )}
      {wire.models && (
        <p className="muted">
          ledgers retained for {wire.models.length} models
          {wire.models.length > 0 && ` (${wire.models
            .map((m) => `${m.model}: ${m.ledger.length} rows`).join(', ')})`} —
          the refusal is the receipt of the attempt.
        </p>
      )}
      <p className="muted">
        A refusal is a recorded outcome, not an error. Adjust the window or
        wait for more data, then run evaluation again.
      </p>
    </section>
  )
}

/** A completed evaluation receipt: headline tally, per-model metrics
 * table (coverage always with n), and the forward fan. */
function OutlookReceiptBlock({
  response,
  quantileGrid,
  wilsonNote,
}: {
  response: ForecastRunResultResponse
  quantileGrid: number[]
  wilsonNote: string | null
}): JSX.Element {
  const w = response.result
  // Double gate, semantic and structural: a refusal record must never
  // render as a receipt (and vice versa the receipt branch is the only
  // place a fan may appear).
  if (!w || w.refusal !== null || w.schema !== 'research-forecast-result/1') {
    return <></>
  }
  const origins = w.origins
  const failedTotal = Object.values(origins.failed_by_model ?? {})
  return (
    <>
      <section className="research-outlook__receipt" data-testid="outlook-receipt">
        <h3>
          Receipt <code>{response.run_id.slice(0, 12)}…</code> —{' '}
          {w.source} · h={w.horizon} · {w.evaluation_window.start}
          {w.evaluation_window.end ? `…${w.evaluation_window.end}` : '…'}
        </h3>
        <p>
          {origins.evaluated} evaluated / {origins.excluded} excluded of{' '}
          {origins.total} origins (floor {origins.floor}:{' '}
          {origins.floor_met ? 'met' : 'not met'}) · evaluation{' '}
          {w.evaluation_status} · calibration {w.calibration_status}
        </p>
        {Object.keys(origins.excluded_reasons).length > 0 && (
          <p className="muted">
            excluded: {Object.entries(origins.excluded_reasons)
              .map(([k, v]) => `${k} ×${v}`).join(', ')}
          </p>
        )}
        {failedTotal.some((n) => n > 0) && (
          <p className="warnings">
            failed origins: {Object.entries(origins.failed_by_model ?? {})
              .filter(([, n]) => n > 0)
              .map(([k, v]) => `${k} ×${v}`).join(', ')}
          </p>
        )}
        <p className="muted">
          series {w.series.series_sha256.slice(0, 12)}… · {w.series.n_sessions}{' '}
          sessions ({w.series.first_session} → {w.series.last_session}) ·{' '}
          {w.series.basis}
        </p>

        <table data-testid="outlook-models-table">
          <thead>
            <tr>
              <th>model</th>
              <th>role</th>
              <th>evaluated</th>
              <th>failed</th>
              <th>grid score</th>
              <th>coverage 90% (with n)</th>
              <th>width 90%</th>
              <th>skill / DM</th>
            </tr>
          </thead>
          <tbody>
            {w.models.map((m) => (
              <tr key={m.model} data-testid={`outlook-model-${m.model}`}>
                <td>{m.model}</td>
                <td>{m.is_baseline ? 'baseline' : 'challenger'}</td>
                <td className="num">{m.n_evaluated}</td>
                <td className="num">
                  {m.n_failed}
                  {Object.keys(m.failure_reasons).length > 0 && (
                    <span className="muted">
                      {' '}({Object.entries(m.failure_reasons)
                        .map(([k, v]) => `${k} ×${v}`).join(', ')})
                    </span>
                  )}
                </td>
                {isDegradedMetrics(m.metrics)
                  ? (
                    <td colSpan={4} className="muted">
                      metrics withheld: {m.metrics.reason}
                      {' '}(n {m.metrics.n_evaluated})
                    </td>
                  )
                  : (
                    <>
                      <td className="num">{fmtLevel(m.metrics.grid_quantile_score, 3)}</td>
                      <td>{coverageLine(m.metrics.coverage_90)}</td>
                      <td className="num">{fmtLevel(m.metrics.mean_width_90, 2)}</td>
                      <td>{skillLine(m.metrics)}</td>
                    </>
                  )}
              </tr>
            ))}
          </tbody>
        </table>
        {wilsonNote && <p className="muted">coverage note: {wilsonNote}</p>}
        <p className="muted">
          primary score {w.study.primary_score} over τ {'['}
          {w.quantile_grid.join(', ')}{']'} ({w.quantile_interpolation}) ·
          benchmark {w.study.benchmark ?? '—'} · access mode{' '}
          {w.study.access_mode}
        </p>
      </section>

      <section aria-label="forward fan" data-testid="outlook-fan">
        <h3>Forward quantiles — latest origin {w.forward.origin_session}</h3>
        <ForecastFanChart
          forward={w.forward}
          quantileGrid={quantileGrid}
          ariaLabel={`forward quantile bands ${w.forward.horizon_sessions} sessions after ${w.forward.origin_session}`}
        />
      </section>
    </>
  )
}
