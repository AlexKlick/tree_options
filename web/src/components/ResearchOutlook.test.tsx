import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ResearchOutlook } from './ResearchOutlook'
import type { ForecastRunResultResponse } from '../lib/types'

// One-shot usePoll everywhere (the first mount fetch resolves the
// fixtures; no fake timers), hoisted vi.mock — the scenarios-tab
// pattern. The run-keying and superseded-POST tests drive deferred
// promises by hand.
//
// FIXTURE DISCIPLINE (checkpoint C, P2-7): every published metric in a
// receipt fixture is COMPUTED from that fixture's own ledger by the
// builder, and every numeric assertion in these tests is RE-DERIVED by
// the independent oracle* helpers below (written here, sharing nothing
// with the builder or the component). A fixture that contradicts its
// ledger cannot exist, and a rendering that shows a different number
// than the ledger implies fails.

const mocks = vi.hoisted(() => ({
  spoolCalls: 0,
  spoolQueue: [] as string[],
  spoolDeferred: null as null | Promise<{ run_id: string; status: string }>,
  resultCalls: [] as string[],
  resultByRun: {} as Record<string, ForecastRunResultResponse | Promise<ForecastRunResultResponse>>,
  runRecords: {} as Record<string, { run_id: string; status: string; kind: string; error?: string }>,
  runDeferred: {} as Record<string, Promise<{ run_id: string; status: string; kind: string }>>,
}))

const A = 'a'.repeat(64)
const B = 'b'.repeat(64)
const TAUS = [0.05, 0.25, 0.5, 0.75, 0.95]

// ---- the independent oracle (assertions re-derive from ledgers) ----

interface OracleRow {
  origin_date: string
  status: string
  actual: number | null
  quantiles: number[]
  losses_by_tau: number[]
}

function oraclePinball(y: number, q: number, tau: number): number {
  return y >= q ? tau * (y - q) : (1 - tau) * (q - y)
}

function oracleModelNumbers(ledger: OracleRow[]): {
  n: number; hits: number; gridScore: number; width: number
} {
  const rows = ledger.filter((r) => r.status === 'evaluated')
  const n = rows.length
  const hits = rows.filter(
    (r) => r.actual !== null
      && r.quantiles[0] <= r.actual && r.actual <= r.quantiles[4]).length
  const perTauMean = TAUS.map((_tau, k) => rows.reduce(
    (s, r) => s + oraclePinball(r.actual as number, r.quantiles[k], TAUS[k]), 0) / n)
  const gridScore = 2 * (perTauMean.reduce((a, b) => a + b, 0) / TAUS.length)
  const width = rows.reduce((s, r) => s + (r.quantiles[4] - r.quantiles[0]), 0) / n
  return { n, hits, gridScore, width }
}

/** Per-origin aggregate loss (the skill/DM cohort unit): 2 x mean-tau. */
function oracleOriginLoss(row: OracleRow): number {
  return 2 * (row.losses_by_tau.reduce((a, b) => a + b, 0) / row.losses_by_tau.length)
}

// ---- the fixture builder (computes its metrics FROM its ledger) ----

function buildPinball(y: number, q: number, tau: number): number {
  return y >= q ? tau * (y - q) : (1 - tau) * (q - y)
}

function originDate(i: number): string {
  const d = new Date(Date.UTC(2023, i, 2))
  return d.toISOString().slice(0, 10)
}
function targetDate(i: number): string {
  const d = new Date(Date.UTC(2023, i, 9))
  return d.toISOString().slice(0, 10)
}

function ledgerRow(
  i: number, status: 'evaluated' | 'failed' | 'excluded',
  actual: number | null, q: number[] | null, reason: string | null,
) {
  const quantiles = q ?? []
  return {
    origin_date: originDate(i),
    target_date: status === 'excluded' && reason === 'target_beyond_data'
      ? null : targetDate(i),
    training_count: 260 + i,
    status,
    reason,
    actual,
    quantiles,
    losses_by_tau: status === 'evaluated' && q && actual !== null
      ? TAUS.map((tau, k) => buildPinball(actual, q[k], tau))
      : [],
  }
}

function coverageBlock(hits: number, n: number) {
  return {
    hits, n, point: n > 0 ? hits / n : null,
    wilson_low: 0.609, wilson_high: 0.947,
    wilson_note: 'binomial approximation; time-ordered origins, dependence not captured',
    bootstrap_low: 0.58, bootstrap_high: 0.89,
    bootstrap_block: 2, bootstrap_seed: 123,
  }
}

function modelMetrics(ledger: OracleRow[], skill: object | null) {
  const rows = ledger.filter((r) => r.status === 'evaluated')
  const n = rows.length
  const perTauMean = TAUS.map((_tau, k) => rows.reduce(
    (s, r) => s + buildPinball(r.actual as number, r.quantiles[k], TAUS[k]), 0) / n)
  return {
    pinball_by_tau: Object.fromEntries(TAUS.map((t, k) => [t.toFixed(2), perTauMean[k]])),
    grid_quantile_score: 2 * (perTauMean.reduce((a, b) => a + b, 0) / TAUS.length),
    coverage_90: coverageBlock(
      rows.filter((r) => r.quantiles[0] <= (r.actual as number)
        && (r.actual as number) <= r.quantiles[4]).length, n),
    mean_width_90: rows.reduce((s, r) => s + (r.quantiles[4] - r.quantiles[0]), 0) / n,
    skill_vs_baseline: skill,
  }
}

function pairedSkill(
  modelLedger: OracleRow[], baselineLedger: OracleRow[],
) {
  // matched = same origin_date, both evaluated (the engine's cohort rule)
  const key = (r: OracleRow) => `${r.origin_date}:${r.status}`
  const matched = modelLedger.filter(
    (r) => r.status === 'evaluated'
      && baselineLedger.some((b) => key(b) === key(r)))
  const n = matched.length
  if (n < 8) {
    return {
      baseline: 'rw_full', paired_n: n, loss_paired: null, bench_paired: null,
      pinball_skill: null, reason: 'paired_cohort_insufficient', dm: null,
      dm_unavailable_reason: 'paired_cohort_insufficient',
    }
  }
  const loss = matched.reduce((s, r) => s + oracleOriginLoss(r), 0) / n
  const bench = matched.reduce((s, r) => {
    const b = baselineLedger.find((x) => key(x) === key(r)) as OracleRow
    return s + oracleOriginLoss(b)
  }, 0) / n
  return {
    baseline: 'rw_full', paired_n: n, loss_paired: loss, bench_paired: bench,
    pinball_skill: 1 - loss / bench, reason: null,
    dm: {
      n, mean: 0.05, stat: 1.85, p_one_sided: 0.032, lag: 1,
      lag_units: 'origin_index', direction: 'baseline loss - model loss',
      series_note: 'matched evaluated origins', sensitivity: {},
    },
  }
}

/** Receipts A and B differ by a level SHIFT (and shifted fans), so the
 * run-keying oracle can tell them apart by rendered numbers. */
function receiptWire(runId: string, shift: number) {
  // The REALIZED close at an origin is a property of the SERIES —
  // identical across models for the same origin/target. Only each
  // model's QUANTILES differ (and therefore who covers whom).
  const actualOf = (i: number): number => 14 + (i % 7) + shift
  const baseQ = [12, 14, 15, 16.5, 19].map((v) => v + shift)
  const winQ = [11.5, 13.5, 15, 16, 17.5].map((v) => v + shift)
  const ar1Q = [12.5, 14.2, 15, 15.8, 18.5].map((v) => v + shift)

  const baseLedger = Array.from({ length: 19 }, (_, i) => ledgerRow(
    i, 'evaluated', actualOf(i), baseQ, null))
  // rw_window: evaluated on origins 0..16, FAILED at 17, EXCLUDED
  // (target beyond data) at 18
  const winLedger = [
    ...Array.from({ length: 17 }, (_, j) => ledgerRow(
      j, 'evaluated', actualOf(j), winQ, null)),
    ledgerRow(17, 'failed', null, null, 'non_finite'),
    ledgerRow(18, 'excluded', null, null, 'target_beyond_data'),
  ]
  // ar1_direct: evaluated on 3 origins only, rest excluded
  const ar1Ledger = [
    ...Array.from({ length: 3 }, (_, j) => ledgerRow(
      j, 'evaluated', actualOf(j), ar1Q, null)),
    ...Array.from({ length: 16 }, (_, j) => ledgerRow(
      j + 3, 'excluded', null, null, 'insufficient_history')),
  ]

  const fan = (c: number) => [
    { model: 'rw_full', status: 'ok', quantiles: {
      '0.05': c - 3.6, '0.25': c - 1.7, '0.50': c, '0.75': c + 1.6, '0.95': c + 4.1 } },
    // rw_window's median sits clearly ABOVE rw_full's (c + 1.2) so the
    // geometry oracle's inverted-y ordering is robust even on a cold
    // zero-width first measure
    { model: 'rw_window', status: 'ok', quantiles: {
      '0.05': c - 3.3, '0.25': c - 1.5, '0.50': c + 1.2, '0.75': c + 2.6, '0.95': c + 4.3 } },
    { model: 'ar1_direct', status: 'unavailable' },
  ]
  const center = 14.8 + shift
  const lastClose = 14.82 + shift

  return {
    run_id: runId,
    status: 'completed',
    result: {
      schema: 'research-forecast-result/1',
      source: 'synthetic-forecast-v1',
      source_basis: 'frozen fixture, sha-pinned',
      grid_basis: 'synthetic pinned-calendar sessions',
      horizon: 5,
      quantile_grid: TAUS,
      quantile_interpolation: 'linear',
      series: {
        n_sessions: 480, first_session: '2019-01-02', last_session: '2024-12-02',
        series_sha256: 's'.repeat(64), basis: 'frozen fixture, sha-pinned',
        grid_basis: 'synthetic pinned-calendar sessions', provenance: {},
        excluded_rows: {}, n_source_rows: 480,
      },
      evaluation_window: { start: '2023-01-01', end: null },
      study: {
        schema: 'research-forecast-study/1', estimand: 'distributional forecast quality',
        target: 'close at the h-th grid session after the origin',
        data_vintage: {}, windows: { evaluation_start: '2023-01-01', evaluation_end: null },
        models: ['rw_full', 'rw_window', 'ar1_direct'], benchmark: 'rw_full',
        primary_score: 'grid_quantile_score', inference: {},
        model_notes: {}, access_mode: 'exploratory',
      },
      execution_status: 'completed',
      evaluation_status: 'receipt_published',
      calibration_status: 'not_claimed',
      origins: {
        total: 19, evaluated: 18, excluded: 1,
        excluded_reasons: { target_beyond_data: 1 },
        floor: 12, floor_met: true,
        failed_by_model: { rw_full: 0, rw_window: 1, ar1_direct: 0 },
      },
      models: [
        { model: 'rw_full', is_baseline: true, n_evaluated: 19, n_failed: 0,
          failure_reasons: {}, metrics: modelMetrics(baseLedger, null),
          ledger: baseLedger },
        { model: 'rw_window', is_baseline: false, n_evaluated: 17, n_failed: 1,
          failure_reasons: { non_finite: 1 },
          metrics: modelMetrics(winLedger, pairedSkill(winLedger, baseLedger)),
          ledger: winLedger },
        { model: 'ar1_direct', is_baseline: false, n_evaluated: 3, n_failed: 0,
          failure_reasons: {},
          metrics: modelMetrics(ar1Ledger, pairedSkill(ar1Ledger, baseLedger)),
          ledger: ar1Ledger },
      ],
      forward: {
        origin_session: '2024-12-02', last_close: lastClose, horizon_sessions: 5,
        beyond_data: true, target_session: null,
        fan: fan(center),
      },
      refusal: null,
    },
    result_sha256: 'r'.repeat(64),
    engine_sha256: 'e'.repeat(64),
    calendar_sha256: 'c'.repeat(64),
    session_authority_sha256: 'y'.repeat(64),
  } as unknown as ForecastRunResultResponse
}

const RECEIPT_A = receiptWire(A, 0)
const RECEIPT_B = receiptWire(B, 6.5)

// The fixture ledgers, recovered for the oracle (single source: the
// builder's own arrays, read back through the published wire).
const LEDGERS_A = (RECEIPT_A.result as { models: Array<{ model: string; ledger: OracleRow[] }> })
  .models.reduce<Record<string, OracleRow[]>>(
    (acc, m) => ({ ...acc, [m.model]: m.ledger }), {})

const REFUSAL = {
  // served under run A: the envelope's run id must MATCH the run it
  // renders under (the component's run-keying guard drops mismatches)
  run_id: A,
  status: 'completed',
  result: {
    schema: 'research-forecast-refusal/1',
    refusal: 'research.forecast.insufficient_origins',
    message: 'horizon 5 on synthetic-forecast-v1: rw_full evaluated 6 origins (< floor 12)',
    source: 'synthetic-forecast-v1',
    horizon: 5,
    origins: {
      total: 6, evaluated: 6, excluded: 0, excluded_reasons: {},
      floor: 12, floor_met: false,
    },
    models: [
      { model: 'rw_full', tally: {}, ledger: new Array(6) },
      { model: 'rw_window', tally: {}, ledger: new Array(6) },
      { model: 'ar1_direct', tally: {}, ledger: new Array(6) },
    ],
  },
} as unknown as ForecastRunResultResponse

function enabledEntry(horizon: number, receipt: string | null, fresh: boolean | null) {
  return {
    horizon, enabled: true as const, latest_receipt_run_id: receipt,
    receipt_series_sha256: receipt ? 's' : null, current_series_sha256: 's',
    receipt_engine_sha256: receipt ? 'e' : null, current_engine_sha256: 'e',
    receipt_calendar_sha256: receipt ? 'c' : null, current_calendar_sha256: 'c',
    receipt_session_authority_sha256: receipt ? 'y' : null,
    current_session_authority_sha256: 'y', fresh,
  }
}

// NOTE: quantile_grid deliberately DIFFERS from the receipt's grid —
// the chart must interpret the receipt's OWN grid, never this one
// (checkpoint C, P2-4).
const METADATA = {
  schema: 'research-forecast-metadata/1',
  quantile_grid: [0.1, 0.5, 0.9],
  origin_floor: 12,
  min_history_sessions: 260,
  paired_floor: 8,
  interval_semantics: 'Bands are quantiles of the modeled distribution; NOT a confidence interval and NOT a claim of calibration.',
  sources: [
    {
      source: 'synthetic-forecast-v1',
      label: 'Synthetic forecast fixture (machinery validation)',
      basis: 'frozen fixture, sha-pinned',
      grid_basis: 'synthetic pinned-calendar sessions',
      horizons: [enabledEntry(5, null, null)],
    },
    {
      source: 'index:VIX',
      label: 'VIX (CBOE via desk-store)',
      basis: 'desk-store latest revision (latest-vintage retrospective)',
      grid_basis: 'observed vendor dates intersected with closure-corrected sessions',
      horizons: [
        enabledEntry(5, 'r-idx-5', true),
        enabledEntry(20, null, null),
        {
          horizon: 63, enabled: false as const, status: 'illustrative_only' as const,
          status_copy: 'not enabled - no evaluation receipt (illustrative only)',
        },
        {
          horizon: 126, enabled: false as const, status: 'illustrative_only' as const,
          status_copy: 'not enabled - no evaluation receipt (illustrative only)',
        },
      ],
    },
  ],
}

vi.mock('../lib/api', () => ({
  getForecastMetadata: async () => METADATA,
  getForecastRun: async (run_id: string) => {
    // a deferred status promise (when set) holds that run's lifecycle
    // response pending — used to keep a stale completed record cached
    const deferred = mocks.runDeferred[run_id]
    if (deferred !== undefined) return deferred
    return mocks.runRecords[run_id] ?? {
      run_id, status: 'completed', spec_hash: run_id, kind: 'forecast',
    }
  },
  getForecastResult: async (run_id: string) => {
    mocks.resultCalls.push(run_id)
    const r = mocks.resultByRun[run_id]
    if (r !== undefined) return r
    return RECEIPT_A
  },
  spoolForecast: async () => {
    mocks.spoolCalls += 1
    const run_id = mocks.spoolQueue.length > 0
      ? mocks.spoolQueue.shift() as string
      : A
    if (mocks.spoolDeferred !== null && mocks.spoolCalls === 1) {
      return mocks.spoolDeferred as Promise<{ run_id: string; status: string }>
    }
    return {
      run_id, status: 'queued', spec_hash: run_id, kind: 'forecast' as const,
      series_sha256: 's'.repeat(64), workspace: '/tmp',
    }
  },
}))

async function runEvaluation(): Promise<void> {
  fireEvent.change(screen.getByTestId('outlook-eval-start'), {
    target: { value: '2023-01-01' },
  })
  fireEvent.click(screen.getByTestId('outlook-run'))
}

describe('ResearchOutlook (RL-3)', () => {
  beforeEach(() => {
    mocks.spoolCalls = 0
    mocks.spoolQueue = []
    mocks.spoolDeferred = null
    mocks.resultCalls = []
    mocks.resultByRun = {}
    mocks.runRecords = {}
    mocks.runDeferred = {}
  })

  it('renders the registry: disabled horizons show their exact copy and are not selectable', async () => {
    render(<ResearchOutlook />)
    await waitFor(() => {
      const source = screen.getByTestId('outlook-source-select') as HTMLSelectElement
      expect([...source.options].some(option => option.value === 'index:VIX')).toBe(true)
    })
    fireEvent.change(screen.getByTestId('outlook-source-select'), {
      target: { value: 'index:VIX' },
    })
    const select = screen.getByTestId('outlook-horizon-select') as HTMLSelectElement
    const opt63 = [...select.options].find((o) => o.value === '63')
    const opt126 = [...select.options].find((o) => o.value === '126')
    expect(opt63?.disabled).toBe(true)
    expect(opt126?.disabled).toBe(true)
    expect(opt63?.textContent).toContain(
      'not enabled - no evaluation receipt (illustrative only)')
    const opt5 = [...select.options].find((o) => o.value === '5')
    expect(opt5?.disabled).toBeFalsy()
  })

  it('renders the interval-semantics text from the registry (the §10 UI row)', async () => {
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-semantics')).toBeTruthy()
    })
    // VERBATIM rendering of the registry string, not a paraphrase that
    // happens to contain one distinctive phrase
    expect(screen.getByTestId('outlook-semantics').textContent)
      .toContain(METADATA.interval_semantics)
  })

  it('a horizon without a receipt shows the run-evaluation empty state and NO fan', async () => {
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-empty')).toBeTruthy()
    })
    expect(screen.getByTestId('outlook-freshness').textContent)
      .toContain('No recorded receipt for this horizon.')
    expect(screen.queryByTestId('outlook-fan')).toBeNull()
    expect(screen.queryByTestId('forecast-fan-svg')).toBeNull()
    expect(screen.getByTestId('outlook-empty').textContent)
      .toContain('run evaluation to publish one')
  })

  it('renders a receipt whose numbers re-derive from its own ledger (independent oracle)', { timeout: 15_000 }, async () => {
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-run')).toBeTruthy()
    })
    await runEvaluation()
    await waitFor(() => {
      expect(screen.getByTestId('outlook-receipt')).toBeTruthy()
    }, { timeout: 8_000 })

    for (const model of ['rw_full', 'rw_window', 'ar1_direct']) {
      const o = oracleModelNumbers(LEDGERS_A[model])
      const row = screen.getByTestId(`outlook-model-${model}`)
      // counts, hits, score, width: re-derived HERE from the ledger
      expect(row.textContent).toContain(`${o.hits}/${o.n}`)
      expect(row.textContent).toContain(o.gridScore.toFixed(3))
      expect(row.textContent).toContain(o.width.toFixed(2))
    }
    // the skill percent is RE-DERIVED here with freshly written code
    // (not the builder's pairedSkill): matched = same origin, both
    // models evaluated; per-origin loss = 2 x mean-tau; skill = 1 -
    // loss/bench on the matched cohort
    const evalRows = (m: string) => LEDGERS_A[m].filter((r) => r.status === 'evaluated')
    const windowDates = new Set(evalRows('rw_window').map((r) => r.origin_date))
    const matched = evalRows('rw_full').filter((r) => windowDates.has(r.origin_date))
    const perOrigin = (r: OracleRow) =>
      2 * (r.losses_by_tau.reduce((a, b) => a + b, 0) / r.losses_by_tau.length)
    const winByDate = new Map(evalRows('rw_window').map((r) => [r.origin_date, r]))
    const loss = matched.reduce((s, r) => s + perOrigin(
      winByDate.get(r.origin_date) as OracleRow), 0) / matched.length
    const bench = matched.reduce((s, r) => s + perOrigin(r), 0) / matched.length
    const winRow = screen.getByTestId('outlook-model-rw_window')
    expect(winRow.textContent).toContain(`on ${matched.length} paired origins`)
    expect(winRow.textContent).toContain(`${(100 * (1 - loss / bench)).toFixed(1)}%`)

    // coverage intervals + caveat
    expect(screen.getByTestId('outlook-model-rw_full').textContent)
      .toContain('Wilson 95% [0.609, 0.947]')
    expect(screen.getByTestId('outlook-model-rw_full').textContent)
      .toContain('bootstrap [0.580, 0.890]')
    expect(document.body.textContent).toContain('binomial approximation')

    // excluded origins and per-model failures are visible
    expect(screen.getByTestId('outlook-receipt').textContent)
      .toContain('target_beyond_data ×1')
    expect(screen.getByTestId('outlook-model-rw_window').textContent)
      .toContain('non_finite ×1')

    // DM carries direction, lag, and lag units; null skill shows reason
    expect(winRow.textContent).toContain('lag 1 origin_index')
    expect(winRow.textContent).toContain('baseline loss - model loss')
    expect(screen.getByTestId('outlook-model-ar1_direct').textContent)
      .toContain('paired_cohort_insufficient')

    // baseline labeled; calibration exactly as recorded
    expect(screen.getByTestId('outlook-model-rw_full').textContent)
      .toContain('baseline')
    expect(screen.getByTestId('outlook-receipt').textContent)
      .toContain('calibration not_claimed')

    // the fan interprets the RECEIPT's grid (5 taus), not the
    // metadata's 3-tau grid; unavailable models are explicit gaps
    expect(screen.getByTestId('outlook-fan')).toBeTruthy()
    const barFull = document.querySelector('[data-testid="forecast-fan-bar-rw_full"]')
    expect(barFull).toBeTruthy()
    expect(screen.getByTestId('outlook-fan').textContent).toContain('14.80')
    expect(document.querySelector('[data-testid="forecast-fan-bar-ar1_direct"]')).toBeNull()
    expect(document.body.textContent).toContain('ar1_direct: unavailable')

    // the fan caption states the beyond-data honesty and the
    // not-a-confidence-interval semantics — removing it must fail
    expect(screen.getByTestId('outlook-fan').textContent)
      .toContain('the target session lies beyond the observed data')
    expect(screen.getByTestId('outlook-fan').textContent)
      .toContain('not confidence intervals')

    // GEOMETRY (derived from the published fan numbers, scale-free):
    // the outer band rect has positive height; the median line sits
    // strictly INSIDE the band; and orientation is inverted-y — the
    // model with the HIGHER published median (rw_window 14.9 vs
    // rw_full 14.8) draws its median line at a SMALLER y
    const rect = barFull?.querySelector('rect')
    const medianLine = barFull?.querySelector('line')
    const ry = parseFloat(rect?.getAttribute('y') ?? '')
    const rh = parseFloat(rect?.getAttribute('height') ?? '')
    const my = parseFloat(medianLine?.getAttribute('y1') ?? '')
    expect(Number.isFinite(rh) && rh).toBeGreaterThan(0)
    expect(my).toBeGreaterThan(ry)
    expect(my).toBeLessThan(ry + rh)
    const winMedianY = parseFloat(
      document.querySelector('[data-testid="forecast-fan-bar-rw_window"] line')
        ?.getAttribute('y1') ?? '')
    expect(winMedianY).toBeLessThan(my)

    // honesty copy: no "calibrate"/"calibrated" anywhere
    expect(document.body.textContent ?? '').not.toMatch(/\bcalibrate(d)?\b/)
  })

  it('renders a typed refusal as the honest blocker with tally and ledger counts — never a fan', { timeout: 15_000 }, async () => {
    mocks.resultByRun[A] = REFUSAL
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-run')).toBeTruthy()
    })
    await runEvaluation()
    await waitFor(() => {
      expect(screen.getByTestId('outlook-refusal')).toBeTruthy()
    }, { timeout: 8_000 })
    expect(screen.getByTestId('outlook-refusal').textContent)
      .toContain('research.forecast.insufficient_origins')
    expect(screen.getByTestId('outlook-refusal').textContent)
      .toContain('6 evaluated / 0 excluded of 6')
    expect(screen.getByTestId('outlook-refusal').textContent)
      .toContain('ledgers retained for 3 models')
    expect(screen.getByTestId('outlook-refusal').textContent)
      .toContain('rw_full: 6 rows')
    expect(screen.queryByTestId('outlook-fan')).toBeNull()
    expect(screen.queryByTestId('outlook-receipt')).toBeNull()
  })

  it('a failed run surfaces the worker error from the run record', { timeout: 15_000 }, async () => {
    mocks.runRecords[A] = {
      run_id: A, status: 'failed', kind: 'forecast',
      error: 'RunstateStoreError: candidates left the catalog since submission',
    }
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-run')).toBeTruthy()
    })
    await runEvaluation()
    await waitFor(() => {
      expect(screen.getByTestId('outlook-failure')).toBeTruthy()
    }, { timeout: 8_000 })
    expect(screen.getByTestId('outlook-failure').textContent)
      .toContain('candidates left the catalog')
    expect(screen.queryByTestId('outlook-receipt')).toBeNull()
    expect(screen.queryByTestId('outlook-fan')).toBeNull()
  })

  it('an idempotent same-id resubmit keeps the recorded receipt on screen', { timeout: 15_000 }, async () => {
    mocks.spoolQueue = [A, A]
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-run')).toBeTruthy()
    })
    await runEvaluation()
    await waitFor(() => {
      expect(screen.getByTestId('outlook-receipt')).toBeTruthy()
    }, { timeout: 8_000 })
    // resubmit the unchanged spec: HTTP 200, same run id — the receipt
    // must NOT vanish (the effect deps would never change again)
    fireEvent.click(screen.getByTestId('outlook-run'))
    await waitFor(() => {
      expect(screen.getByTestId('outlook-receipt')).toBeTruthy()
    }, { timeout: 8_000 })
    expect(mocks.resultCalls.filter((id) => id === A).length).toBe(1)
  })

  it('a superseded POST is discarded: switching lane while it is pending never restores the old run', { timeout: 15_000 }, async () => {
    let resolveSpool: (v: { run_id: string; status: string }) => void = () => { }
    mocks.spoolDeferred = new Promise((res) => { resolveSpool = res })
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-run')).toBeTruthy()
    })
    await runEvaluation()          // POST for synthetic run A is pending
    // the operator switches to the VIX lane while it is in flight
    fireEvent.change(screen.getByTestId('outlook-source-select'), {
      target: { value: 'index:VIX' },
    })
    resolveSpool({ run_id: A, status: 'queued' })
    await waitFor(() => {
      expect((screen.getByTestId('outlook-source-select') as HTMLSelectElement).value)
        .toBe('index:VIX')
    })
    // the stale response landed AFTER the switch: nothing of run A may
    // appear under the VIX controls
    await new Promise((r) => setTimeout(r, 150))
    expect(screen.queryByTestId('outlook-status')).toBeNull()
    expect(screen.queryByTestId('outlook-receipt')).toBeNull()
    expect(screen.queryByTestId('outlook-fan')).toBeNull()
  })

  it('polled-status identity gate: a cached COMPLETED status for run A cannot trigger run B\'s result fetch', { timeout: 15_000 }, async () => {
    // The poll keeps A's completed record while runId has moved to B.
    // Without the run.run_id === runId gate, the stale 'completed'
    // status would fire B's one-shot result fetch while B is still
    // queued (the real endpoint would return {status:'queued',
    // result:null}) and B's receipt could never load.
    mocks.spoolQueue = [A, B]
    mocks.resultByRun[B] = RECEIPT_B
    let resolveBStatus: (v: { run_id: string; status: string; kind: string }) => void = () => { }
    mocks.runDeferred[B] = new Promise((res) => { resolveBStatus = res })
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-run')).toBeTruthy()
    })
    await runEvaluation()   // A completes through the default route
    await waitFor(() => {
      expect(mocks.resultCalls).toContain(A)
    })
    await runEvaluation()   // supersede with B; B's status stays pending
    await new Promise((r) => setTimeout(r, 150))
    // the stale A-completed status must NOT have fetched B's result
    expect(mocks.resultCalls).not.toContain(B)
    expect(screen.queryByTestId('outlook-receipt')).toBeNull()
    // B's status arrives → the CURRENT run's identity gates the fetch
    resolveBStatus({ run_id: B, status: 'completed', kind: 'forecast' })
    await waitFor(() => {
      expect(mocks.resultCalls).toContain(B)
    }, { timeout: 8_000 })
    await waitFor(() => {
      expect(screen.getByTestId('outlook-receipt')).toBeTruthy()
    }, { timeout: 8_000 })
  })

  it('run-keying: a slow response for run A can never render under run B\'s selection', { timeout: 15_000 }, async () => {
    let resolveA: (v: ForecastRunResultResponse) => void = () => { }
    const deferredA = new Promise<ForecastRunResultResponse>((res) => { resolveA = res })
    mocks.spoolQueue = [A, B]
    mocks.resultByRun[A] = deferredA
    mocks.resultByRun[B] = RECEIPT_B
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-run')).toBeTruthy()
    })
    await runEvaluation()                       // spools run A
    // A's result REQUEST must actually start (its deferred exists)
    // BEFORE B supersedes it — otherwise "A never rendered" is trivial
    await waitFor(() => {
      expect(mocks.resultCalls).toContain(A)
    })
    await runEvaluation()                       // supersedes with run B
    await waitFor(() => {
      expect(screen.getByTestId('outlook-receipt')).toBeTruthy()
    }, { timeout: 8_000 })
    // A and B differ only by a level SHIFT (losses and coverage are
    // translation-invariant), so the fan LEVELS are the discriminator:
    // B's fan median is 21.30, A's is 14.80
    expect(screen.getByTestId('outlook-fan').textContent).toContain('21.30')
    resolveA(RECEIPT_A)
    await new Promise((r) => setTimeout(r, 150))
    // A's fan levels never render under B's selection
    expect(screen.getByTestId('outlook-fan').textContent).not.toContain('14.80')
    expect(screen.getByTestId('outlook-fan').textContent).toContain('21.30')
  })
})
