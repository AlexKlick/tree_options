import { describe, expect, it } from 'vitest'
import {
  coverageLine,
  fanLevels,
  fmtLevel,
  fmtPct,
  hasReceipt,
  horizonEntry,
  isDegradedMetrics,
  isForecastRefusal,
  skillLine,
} from './forecast'
import type {
  ForecastCoverage90,
  ForecastMetadata,
  ForecastModelMetrics,
  ForecastRunResultResponse,
} from './types'

const GRID = [0.05, 0.25, 0.5, 0.75, 0.95]

describe('isForecastRefusal', () => {
  it('distinguishes a typed refusal record from a receipt', () => {
    expect(isForecastRefusal({ refusal: 'research.forecast.source_drift' })).toBe(true)
    expect(isForecastRefusal({ refusal: null })).toBe(false)
    expect(isForecastRefusal(null)).toBe(false)
    expect(isForecastRefusal(undefined)).toBe(false)
  })
})

describe('isDegradedMetrics', () => {
  it('matches only the withheld-with-reason shape', () => {
    expect(isDegradedMetrics({
      aggregate_status: 'non_finite',
      reason: 'overflow',
      n_evaluated: 19,
    })).toBe(true)
    expect(isDegradedMetrics({
      pinball_by_tau: {},
      grid_quantile_score: 1,
      coverage_90: {} as ForecastCoverage90,
      mean_width_90: 1,
      skill_vs_baseline: null,
    })).toBe(false)
  })
})

describe('coverageLine — coverage is always quoted with n', () => {
  const base: ForecastCoverage90 = {
    hits: 15,
    n: 19,
    point: 15 / 19,
    wilson_low: 0.581,
    wilson_high: 0.909,
    wilson_note: 'binomial approximation',
    bootstrap_low: 0.42,
    bootstrap_high: 0.87,
    bootstrap_block: 2,
    bootstrap_seed: 12345,
  }
  it('renders hits/n, the point estimate, and both intervals', () => {
    const line = coverageLine(base)
    expect(line).toContain('15/19')
    expect(line).toContain('(78.9%)')        // 15/19 = 0.78947… -> 78.9%
    expect(line).toContain('Wilson 95% [0.581, 0.909]')
    expect(line).toContain('bootstrap [0.420, 0.870]')
  })
  it('n=0 renders 0/0 with no percentage, never NaN', () => {
    const line = coverageLine({
      ...base, hits: 0, n: 0, point: null,
      wilson_low: null, wilson_high: null,
    })
    expect(line).toContain('0/0')
    expect(line).toContain('Wilson 95% —')
    expect(line).not.toContain('NaN')
    // no point-estimate group like "(0.0%)" when there is no n behind it
    expect(line).not.toMatch(/\(\d/)
  })
  it('a degenerate bootstrap renders its reason, not [null, null]', () => {
    const line = coverageLine({
      ...base, bootstrap_low: null, bootstrap_high: null,
      bootstrap_reason: 'degenerate_indicators',
    })
    expect(line).toContain('bootstrap: degenerate_indicators')
    expect(line).not.toContain('[null')
  })
})

describe('fanLevels — the grid orders the fan, never key order', () => {
  it('aligns levels to the declared grid when keys are inserted out of order', () => {
    const levels = fanLevels({
      model: 'rw_full', status: 'ok',
      // '0.95' first: insertion order must not become the level order;
      // keys are two-decimal strings ('0.50'), matching f"{t:.2f}" wire-side
      quantiles: { '0.95': 24, '0.05': 10, '0.25': 14, '0.50': 17, '0.75': 20 },
    }, GRID)
    expect(levels).toEqual([10, 14, 17, 20, 24])
  })
  it('an unavailable fan entry is all nulls — a gap, not an invented level', () => {
    expect(fanLevels({ model: 'ar1_direct', status: 'unavailable' }, GRID))
      .toEqual([null, null, null, null, null])
  })
  it('a missing key or a non-finite value yields null in that slot only', () => {
    const levels = fanLevels({
      model: 'rw_window', status: 'ok',
      quantiles: { '0.05': 9, '0.25': 13, '0.50': 16, '0.75': 21 },
    }, GRID)
    expect(levels).toEqual([9, 13, 16, 21, null])
    expect(fanLevels({
      model: 'rw_window', status: 'ok',
      quantiles: { '0.05': 9, '0.25': 13, '0.50': Infinity, '0.75': 21, '0.95': 30 },
    }, GRID)).toEqual([9, 13, null, 21, 30])
  })
})

describe('horizonEntry', () => {
  const metadata: ForecastMetadata = {
    schema: 'research-forecast-metadata/1',
    quantile_grid: GRID,
    origin_floor: 12,
    min_history_sessions: 260,
    paired_floor: 8,
    interval_semantics: 'bands are quantiles',
    sources: [
      {
        source: 'synthetic-forecast-v1', label: 'synthetic', basis: 'fixture',
        grid_basis: 'pinned', horizons: [
          {
            horizon: 5, enabled: true, latest_receipt_run_id: null,
            receipt_series_sha256: null, current_series_sha256: 'a',
            receipt_engine_sha256: null, current_engine_sha256: 'b',
            receipt_calendar_sha256: null, current_calendar_sha256: 'c',
            receipt_session_authority_sha256: null,
            current_session_authority_sha256: 'd', fresh: null,
          },
        ],
      },
      {
        source: 'index:VIX', label: 'VIX', basis: 'latest-vintage',
        grid_basis: 'observed ∩ closure-corrected', horizons: [
          {
            horizon: 5, enabled: true, latest_receipt_run_id: 'r1',
            receipt_series_sha256: 'x', current_series_sha256: 'x',
            receipt_engine_sha256: 'e', current_engine_sha256: 'e',
            receipt_calendar_sha256: 'c', current_calendar_sha256: 'c',
            receipt_session_authority_sha256: 's',
            current_session_authority_sha256: 's', fresh: true,
          },
          {
            horizon: 63, enabled: false, status: 'illustrative_only',
            status_copy: 'not enabled - no evaluation receipt (illustrative only)',
          },
        ],
      },
    ],
  }
  it('finds enabled and illustrative entries alike', () => {
    expect(horizonEntry(metadata, 'index:VIX', 5)?.enabled).toBe(true)
    const illus = horizonEntry(metadata, 'index:VIX', 63)
    expect(illus?.enabled).toBe(false)
    expect(illus && 'status' in illus ? illus.status : undefined)
      .toBe('illustrative_only')
  })
  it('returns undefined for an unlisted horizon or source', () => {
    expect(horizonEntry(metadata, 'index:VIX', 200)).toBeUndefined()
    expect(horizonEntry(metadata, 'nope', 5)).toBeUndefined()
    expect(horizonEntry(null, 'index:VIX', 5)).toBeUndefined()
  })
})

describe('hasReceipt — the no-fan precondition', () => {
  const receipt = {
    run_id: 'r1', status: 'completed',
    result: { schema: 'research-forecast-result/1', refusal: null },
  } as unknown as ForecastRunResultResponse
  it('true only for a completed run carrying a receipt', () => {
    expect(hasReceipt(receipt)).toBe(true)
  })
  it('false for a refusal, a pending run, or nothing', () => {
    expect(hasReceipt({
      run_id: 'r2', status: 'completed',
      result: { refusal: 'research.forecast.insufficient_origins' },
    } as unknown as ForecastRunResultResponse)).toBe(false)
    expect(hasReceipt({
      run_id: 'r3', status: 'queued', result: null,
    } as unknown as ForecastRunResultResponse)).toBe(false)
    expect(hasReceipt(null)).toBe(false)
  })
})

describe('skillLine', () => {
  const metrics = (skill: ForecastModelMetrics['skill_vs_baseline']): ForecastModelMetrics => ({
    pinball_by_tau: {}, grid_quantile_score: 1,
    coverage_90: {} as ForecastCoverage90, mean_width_90: 1,
    skill_vs_baseline: skill,
  })
  it('the baseline row says baseline', () => {
    expect(skillLine(metrics(null))).toBe('baseline')
  })
  it('a null skill shows its reason, never a number', () => {
    expect(skillLine(metrics({
      baseline: 'rw_full', paired_n: 3, loss_paired: null, bench_paired: null,
      pinball_skill: null, reason: 'paired_cohort_insufficient', dm: null,
    }))).toBe('paired_cohort_insufficient')
  })
  it('a published skill carries n, DM stat, lag units, and direction', () => {
    const line = skillLine(metrics({
      baseline: 'rw_full', paired_n: 19, loss_paired: 0.9, bench_paired: 1.1,
      pinball_skill: 0.1818, reason: null,
      dm: {
        n: 19, mean: 0.2, stat: 1.5, p_one_sided: 0.0668, lag: 1,
        lag_units: 'origin_index', direction: 'baseline loss - model loss',
        series_note: '', sensitivity: {},
      },
    }))
    expect(line).toContain('18.2%')
    expect(line).toContain('19 paired origins')
    expect(line).toContain('lag 1 origin_index')
    expect(line).toContain('baseline loss - model loss')
  })
})

describe('number formatting', () => {
  it('nullish and non-finite render as a dash, never NaN', () => {
    expect(fmtPct(null)).toBe('—')
    expect(fmtLevel(undefined)).toBe('—')
    expect(fmtLevel(NaN)).toBe('—')
    expect(fmtPct(0.5)).toBe('50.0%')
    expect(fmtLevel(14.3249)).toBe('14.32')
  })
  it('never formats as currency — this surface is not money', () => {
    expect(fmtLevel(1234.5)).not.toContain('$')
    expect(fmtPct(0.5)).not.toContain('$')
  })
})
