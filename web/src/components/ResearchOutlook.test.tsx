import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ResearchOutlook } from './ResearchOutlook'
import type { ForecastRunResultResponse } from '../lib/types'

// One-shot usePoll everywhere (the first mount fetch resolves the
// fixtures; no fake timers), hoisted vi.mock — the scenarios-tab
// pattern. The run-keying test drives deferred promises by hand.

const mocks = vi.hoisted(() => ({
  spoolCalls: 0,
  resultByRun: {} as Record<string, ForecastRunResultResponse | Promise<ForecastRunResultResponse>>,
}))

const A = 'a'.repeat(64)
const B = 'b'.repeat(64)

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

const METADATA = {
  schema: 'research-forecast-metadata/1',
  quantile_grid: [0.05, 0.25, 0.5, 0.75, 0.95],
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

function modelReceipt(over: {
  model: string
  is_baseline: boolean
  n_evaluated: number
  n_failed?: number
  failure_reasons?: Record<string, number>
  coverage: { hits: number; n: number }
  skill: object | null
}) {
  const { hits, n } = over.coverage
  return {
    model: over.model,
    is_baseline: over.is_baseline,
    n_evaluated: over.n_evaluated,
    n_failed: over.n_failed ?? 0,
    failure_reasons: over.failure_reasons ?? {},
    metrics: {
      pinball_by_tau: { '0.05': 0.4, '0.25': 0.3, '0.50': 0.3, '0.75': 0.4, '0.95': 0.7 },
      grid_quantile_score: 0.842,
      coverage_90: {
        hits, n, point: n > 0 ? hits / n : null,
        wilson_low: 0.609, wilson_high: 0.947,
        wilson_note: 'binomial approximation; time-ordered origins, dependence not captured',
        bootstrap_low: 0.58, bootstrap_high: 0.89,
        bootstrap_block: 2, bootstrap_seed: 123,
      },
      mean_width_90: 6.1,
      skill_vs_baseline: over.skill,
    },
    ledger: Array.from({ length: over.n_evaluated }, (_, i) => ({
      origin_date: `2024-${String((i % 12) + 1).padStart(2, '0')}-01`,
      target_date: `2024-${String((i % 12) + 1).padStart(2, '0')}-08`,
      training_count: 260 + i, status: 'evaluated', reason: null,
      actual: 15 + i, quantiles: [12, 14, 15, 16, 18], losses_by_tau: [0.4, 0.3, 0.3, 0.4, 0.7],
    })),
  }
}

/** Receipts A and B differ in their coverage numbers so the run-keying
 * oracle can tell them apart (A: 16/19, B: 17/19). */
function receiptWire(runId: string, hits: number) {
  return {
    run_id: runId,
    status: 'completed',
    result: {
      schema: 'research-forecast-result/1',
      source: 'synthetic-forecast-v1',
      source_basis: 'frozen fixture, sha-pinned',
      grid_basis: 'synthetic pinned-calendar sessions',
      horizon: 5,
      quantile_grid: [0.05, 0.25, 0.5, 0.75, 0.95],
      quantile_interpolation: 'linear',
      series: {
        n_sessions: 480, first_session: '2019-01-02', last_session: '2024-12-02',
        series_sha256: 's'.repeat(64), basis: 'frozen fixture, sha-pinned',
        grid_basis: 'synthetic pinned-calendar sessions', provenance: {},
        excluded_rows: {}, n_source_rows: 480,
      },
      evaluation_window: { start: '2024-01-01', end: null },
      study: {
        schema: 'research-forecast-study/1', estimand: 'distributional forecast quality',
        target: 'close at the h-th grid session after the origin',
        data_vintage: {}, windows: { evaluation_start: '2024-01-01', evaluation_end: null },
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
        modelReceipt({
          model: 'rw_full', is_baseline: true, n_evaluated: 18,
          coverage: { hits, n: 19 }, skill: null,
        }),
        modelReceipt({
          model: 'rw_window', is_baseline: false, n_evaluated: 17,
          n_failed: 1, failure_reasons: { non_finite: 1 },
          coverage: { hits: 14, n: 17 },
          skill: {
            baseline: 'rw_full', paired_n: 17, loss_paired: 0.79, bench_paired: 0.84,
            pinball_skill: 0.0595, reason: null,
            dm: {
              n: 17, mean: 0.05, stat: 1.85, p_one_sided: 0.032, lag: 1,
              lag_units: 'origin_index', direction: 'baseline loss - model loss',
              series_note: 'matched evaluated origins', sensitivity: {},
            },
          },
        }),
        modelReceipt({
          model: 'ar1_direct', is_baseline: false, n_evaluated: 18,
          coverage: { hits: 15, n: 18 },
          skill: {
            baseline: 'rw_full', paired_n: 3, loss_paired: null, bench_paired: null,
            pinball_skill: null, reason: 'paired_cohort_insufficient', dm: null,
          },
        }),
      ],
      forward: {
        origin_session: '2024-12-02', last_close: 14.82, horizon_sessions: 5,
        beyond_data: true, target_session: null,
        fan: [
          { model: 'rw_full', status: 'ok', quantiles: { '0.05': 11.2, '0.25': 13.1, '0.50': 14.8, '0.75': 16.4, '0.95': 18.9 } },
          { model: 'rw_window', status: 'ok', quantiles: { '0.05': 11.5, '0.25': 13.3, '0.50': 14.9, '0.75': 16.6, '0.95': 19.1 } },
          { model: 'ar1_direct', status: 'unavailable' },
        ],
      },
      refusal: null,
    },
    result_sha256: 'r'.repeat(64),
    engine_sha256: 'e'.repeat(64),
    calendar_sha256: 'c'.repeat(64),
    session_authority_sha256: 'y'.repeat(64),
  } as unknown as ForecastRunResultResponse
}

const RECEIPT_A = receiptWire(A, 16)
const RECEIPT_B = receiptWire(B, 17)

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

vi.mock('../lib/api', () => ({
  getForecastMetadata: async () => METADATA,
  getForecastRun: async (run_id: string) => ({
    run_id, status: 'completed', spec_hash: run_id, kind: 'forecast',
  }),
  getForecastResult: async (run_id: string) => {
    const r = mocks.resultByRun[run_id]
    if (r !== undefined) return r
    return RECEIPT_A
  },
  spoolForecast: async () => {
    mocks.spoolCalls += 1
    const run_id = mocks.spoolCalls === 1 ? A : B
    return {
      run_id, status: 'queued', spec_hash: run_id, kind: 'forecast' as const,
      series_sha256: 's'.repeat(64), workspace: '/tmp',
    }
  },
}))

async function runEvaluation(): Promise<void> {
  fireEvent.change(screen.getByTestId('outlook-eval-start'), {
    target: { value: '2024-01-01' },
  })
  fireEvent.click(screen.getByTestId('outlook-run'))
}

describe('ResearchOutlook (RL-3)', () => {
  // The spool counter decides which run id the NEXT POST returns, so it
  // must start at 0 for every test; per-test result overrides likewise.
  beforeEach(() => {
    mocks.spoolCalls = 0
    mocks.resultByRun = {}
  })

  it('renders the registry: disabled horizons show their exact copy and are not selectable', async () => {
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-source-select')).toBeTruthy()
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

  it('a horizon without a receipt shows the run-evaluation empty state and NO fan', async () => {
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-empty')).toBeTruthy()
    })
    // synthetic h5 has no receipt in the metadata fixture
    expect(screen.getByTestId('outlook-freshness').textContent)
      .toContain('No recorded receipt for this horizon.')
    expect(screen.queryByTestId('outlook-fan')).toBeNull()
    expect(screen.queryByTestId('forecast-fan-svg')).toBeNull()
    // the empty state's copy reads "run evaluation" — never calibrate
    expect(screen.getByTestId('outlook-empty').textContent)
      .toContain('run evaluation to publish one')
  })

  it('renders a completed receipt: coverage with n, Wilson + bootstrap + caveat, excluded/failed visible, DM with direction and lag units, baseline labeled, calibration not claimed, fan present', { timeout: 15_000 }, async () => {
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-run')).toBeTruthy()
    })
    await runEvaluation()
    await waitFor(() => {
      expect(screen.getByTestId('outlook-receipt')).toBeTruthy()
    }, { timeout: 8_000 })
    // coverage ALWAYS with n (A's baseline: 16/19)
    expect(screen.getByTestId('outlook-model-rw_full').textContent)
      .toContain('16/19')
    expect(screen.getByTestId('outlook-model-rw_full').textContent)
      .toContain('Wilson 95% [0.609, 0.947]')
    expect(screen.getByTestId('outlook-model-rw_full').textContent)
      .toContain('bootstrap [0.580, 0.890]')
    // the Wilson caveat is rendered, not swallowed
    expect(document.body.textContent).toContain('binomial approximation')
    // excluded origins and per-model failures are visible
    expect(screen.getByTestId('outlook-receipt').textContent)
      .toContain('target_beyond_data ×1')
    expect(screen.getByTestId('outlook-model-rw_window').textContent)
      .toContain('non_finite ×1')
    // DM carries direction, lag, and lag units
    expect(screen.getByTestId('outlook-model-rw_window').textContent)
      .toContain('lag 1 origin_index')
    expect(screen.getByTestId('outlook-model-rw_window').textContent)
      .toContain('baseline loss - model loss')
    // a null skill shows its reason, never a number
    expect(screen.getByTestId('outlook-model-ar1_direct').textContent)
      .toContain('paired_cohort_insufficient')
    // baseline labeled; calibration exactly as recorded
    expect(screen.getByTestId('outlook-model-rw_full').textContent)
      .toContain('baseline')
    expect(screen.getByTestId('outlook-receipt').textContent)
      .toContain('calibration not_claimed')
    // the fan renders only with the receipt, and an unavailable model
    // is an explicit gap
    expect(screen.getByTestId('outlook-fan')).toBeTruthy()
    expect(document.querySelector('[data-testid="forecast-fan-bar-rw_full"]')).toBeTruthy()
    expect(document.querySelector('[data-testid="forecast-fan-bar-ar1_direct"]')).toBeNull()
    expect(document.body.textContent).toContain('ar1_direct: unavailable')
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

  it('run-keying: a slow response for run A can never render under run B\'s selection', { timeout: 15_000 }, async () => {
    let resolveA: (v: ForecastRunResultResponse) => void = () => { }
    const deferredA = new Promise<ForecastRunResultResponse>((res) => { resolveA = res })
    mocks.resultByRun[A] = deferredA
    mocks.resultByRun[B] = RECEIPT_B
    render(<ResearchOutlook />)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-run')).toBeTruthy()
    })
    await runEvaluation()                       // spools run A
    await runEvaluation()                       // supersedes with run B
    await waitFor(() => {
      expect(screen.getByTestId('outlook-receipt')).toBeTruthy()
    }, { timeout: 8_000 })
    // B's receipt is on screen (17/19), A's (16/19) has not arrived yet
    expect(screen.getByTestId('outlook-model-rw_full').textContent)
      .toContain('17/19')
    expect(screen.getByTestId('outlook-model-rw_full').textContent)
      .not.toContain('16/19')
    // A's slow response lands LAST — it must be dropped, not rendered
    resolveA(RECEIPT_A)
    await waitFor(() => {
      expect(screen.getByTestId('outlook-model-rw_full').textContent)
        .toContain('17/19')
    })
    expect(screen.getByTestId('outlook-model-rw_full').textContent)
      .not.toContain('16/19')
  })
})
