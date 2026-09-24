// Price tab of the symbol page (Phase 2 viewer upgrade): a 60 s poll of
// the desk panel's long-term OHLCV with range chips and a line/candles
// renderer toggle. Every degrade is honest — a busy or unavailable panel
// gets a retry card, and a symbol the panel does not cover yet (pre-backfill
// names like PLTR/SPCX) falls back to the legacy 365-day envelope chart.

import { useEffect, useRef, useState } from 'react'
import { getSymbolHistory } from '../../lib/api'
import { usePoll } from '../../hooks/usePoll'
import { ago } from '../../lib/format'
import type { HistorySeries, SymbolHistory } from '../../lib/types'
import { TimeSeriesChart } from '../TimeSeriesChart'
import { SymbolHistoryChart, type ChartMode } from './SymbolHistoryChart'

const RANGES = [
  { key: '1y', label: '1Y' },
  { key: '3y', label: '3Y' },
  { key: '5y', label: '5Y' },
  { key: 'max', label: 'Max' },
] as const

type RangeKey = (typeof RANGES)[number]['key']

const dateFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  month: 'short',
  day: 'numeric',
})

const ageNote = (seconds: number | null | undefined): string =>
  seconds === null || seconds === undefined ? '' : ` · data ${ago(seconds)} old`

// the price axis fits the data extent, so cheap stocks need cents
const priceLabel = (v: number): string =>
  v >= 100 ? `$${Math.round(v).toLocaleString('en-US')}` : `$${v.toFixed(2)}`

export function PriceHistoryPanel({
  sym,
  bars,
  barsAgeSeconds,
}: {
  sym: string
  bars: HistorySeries | null
  barsAgeSeconds?: number | null
}) {
  const [range, setRange] = useState<RangeKey>('3y')
  const [mode, setMode] = useState<ChartMode>('line')
  // candles read at a reduced point count; the toggle refetches
  const maxPoints = mode === 'candles' ? 160 : 600
  const poll = usePoll(() => getSymbolHistory(sym, range, maxPoints), 60_000)

  // a range/mode change refetches immediately instead of waiting out the
  // 60 s tick (the first mount already fetched, so it is skipped here)
  const refreshRef = useRef(poll.refresh)
  refreshRef.current = poll.refresh
  const wanted = useRef(`${range}:${maxPoints}`)
  useEffect(() => {
    const key = `${range}:${maxPoints}`
    if (wanted.current !== key) {
      wanted.current = key
      refreshRef.current()
    }
  }, [range, maxPoints])

  const h: SymbolHistory | null = poll.data

  if (h === null) {
    return (
      <div className="card empty-state">
        <p className="muted">
          {poll.error ? `Cannot reach the cockpit API. ${poll.error}` : 'Loading price history…'}
        </p>
      </div>
    )
  }

  // degrade: the panel writer holds the lock, or the store is unreadable
  if (h.points === null) {
    return (
      <div className="card empty-state">
        <p className="muted">No price history right now — {h.error}.</p>
        <button type="button" className="chip" onClick={() => poll.refresh()}>
          Retry
        </button>
      </div>
    )
  }

  // pre-backfill names: the panel does not carry this symbol yet
  if (!h.in_panel) {
    return (
      <>
        <h2 className="section-title">
          Daily closes{' '}
          <span className="muted section-sub">
            (~1 year · Polygon delayed{ageNote(barsAgeSeconds)})
          </span>
        </h2>
        {bars ? (
          <div className="card chart-card">
            <TimeSeriesChart
              series={bars}
              ariaLabel={`Daily closes for ${sym}`}
              valueFormat={priceLabel}
              timeFormat={(ts) => dateFmt.format(new Date(ts))}
            />
          </div>
        ) : (
          <div className="card empty-state">
            <p className="muted">No bars cached yet — hit “Refresh data”.</p>
          </div>
        )}
      </>
    )
  }

  const sessions = h.range_sessions ?? h.points.length
  return (
    <>
      <div className="price-controls">
        <div className="chip-row" role="group" aria-label="Range">
          {RANGES.map((r) => (
            <button
              key={r.key}
              type="button"
              className="chip"
              aria-pressed={range === r.key}
              onClick={() => setRange(r.key)}
            >
              {r.label}
            </button>
          ))}
        </div>
        <div className="density-toggle" role="group" aria-label="Renderer">
          <button
            type="button"
            aria-pressed={mode === 'line'}
            onClick={() => setMode('line')}
          >
            Line
          </button>
          <button
            type="button"
            aria-pressed={mode === 'candles'}
            onClick={() => setMode('candles')}
          >
            Candles
          </button>
        </div>
      </div>
      <h2 className="section-title">
        Price{' '}
        <span className="muted section-sub">
          panel through {h.panel_last_session} · split-adjusted · nightly · {sessions}{' '}
          sessions
        </span>
      </h2>
      <div className="card chart-card">
        <SymbolHistoryChart
          points={h.points}
          yLo={h.y_lo ?? 0}
          yHi={h.y_hi ?? 0}
          volMax={h.vol_max ?? 0}
          mode={mode}
          ariaLabel={`Price history for ${sym}`}
        />
      </div>
    </>
  )
}
