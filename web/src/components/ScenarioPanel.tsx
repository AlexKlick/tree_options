// Valuation scenario (M4): "how would this kind of trade have panned
// out?" — the candidate's shape replayed over ~1y of daily closes as
// rolling, moneyness-matched analogs. Hard-labeled simulated; the copy
// says what the spread WOULD BE WORTH, never what you would have earned
// (no exits are modeled, and results never enter real win rates).

import { useEffect, useRef, useState } from 'react'
import { getScenario, requestScenario } from '../lib/api'
import { usdSigned } from '../lib/format'
import { usePoll } from '../hooks/usePoll'
import type { ScenarioStats } from '../lib/types'
import { Pill } from './Pill'
import { TimeSeriesChart } from './TimeSeriesChart'

const dateFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  month: 'short',
  day: 'numeric',
})

const pct = (v: number): string => `${Math.round(v * 100)}%`

function StatsLine({ s }: { s: ScenarioStats }) {
  return (
    <>
      {s.count} windows · win {pct(s.win_rate)} · mean{' '}
      <span className={s.mean_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>{usdSigned(s.mean_pnl)}</span>{' '}
      · median {usdSigned(s.median_pnl)} · p10 {usdSigned(s.p10_pnl)} · worst{' '}
      {usdSigned(s.worst_pnl)} · best {usdSigned(s.best_pnl)}
    </>
  )
}

export function ScenarioPanel({
  scenarioKey,
  title,
  onClose,
}: {
  scenarioKey: string
  title: string
  onClose: () => void
}) {
  const [requestedAt] = useState(() => Date.now())
  const [requestError, setRequestError] = useState<string | null>(null)
  const ref = useRef<HTMLElement>(null)

  useEffect(() => {
    requestScenario(scenarioKey).catch((err: unknown) =>
      setRequestError(err instanceof Error ? err.message : String(err)),
    )
    ref.current?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' })
  }, [scenarioKey])

  const [fresh, setFresh] = useState(false)
  const poll = usePoll(() => getScenario(scenarioKey), fresh ? 60_000 : 3_000)
  const doc = poll.data
  // an artifact from an earlier request may be served until the runner
  // writes the new one; 5s of slack absorbs clock skew
  const isFresh = doc !== null && Date.parse(doc.generated_at) >= requestedAt - 5_000
  useEffect(() => {
    if (isFresh) setFresh(true)
  }, [isFresh])

  return (
    <section className="card scenario-panel" ref={ref} aria-label={`Valuation scenario for ${title}`}>
      <div className="card-head">
        <h3 style={{ margin: 0 }}>Scenario · {title}</h3>
        <button type="button" className="chip" onClick={onClose} aria-label="Close scenario">
          close
        </button>
      </div>
      <div className="pill-row" style={{ margin: '8px 0 12px' }}>
        <Pill variant="disarmed">valuation scenario · approximate · simulated</Pill>
        <Pill variant="empty">not counted in real win rates</Pill>
        {!isFresh && !requestError && <Pill variant="empty">○ runner computing…</Pill>}
      </div>

      {requestError && !doc ? (
        <p className="muted">Could not request the scenario: {requestError}</p>
      ) : doc === null ? (
        <p className="muted">Waiting for the discovery runner…</p>
      ) : doc.error ? (
        <p className="muted">Scenario unavailable: {doc.error}</p>
      ) : (
        <>
          <p style={{ marginTop: 0 }}>
            If this spread's shape (same moneyness, same {doc.structure?.dte}-day hold) had been
            opened at each daily close of the last {doc.sessions} sessions and held to its
            expiry, <strong>per 1 contract</strong>:
          </p>
          {doc.analogs && (
            <p className="num" style={{ margin: '4px 0' }}>
              <StatsLine s={doc.analogs} />
            </p>
          )}
          {doc.pessimistic && (
            <p className="muted num" style={{ margin: '4px 0' }}>
              paying today's ask on every entry: win {pct(doc.pessimistic.win_rate)} · mean{' '}
              {usdSigned(doc.pessimistic.mean_pnl)}
            </p>
          )}
          {doc.iv_band_mean_pnl && doc.iv_band_mean_pnl.lo != null && doc.iv_band_mean_pnl.hi != null && (
            <p className="muted num" style={{ margin: '4px 0' }}>
              vol ±20%: mean {usdSigned(doc.iv_band_mean_pnl.hi)} … {usdSigned(doc.iv_band_mean_pnl.lo)}
            </p>
          )}
          {doc.recent && (
            <>
              <p className="muted" style={{ margin: '14px 0 0' }}>
                Most recent complete window: opened {dateFmt.format(new Date(doc.recent.entry_ms))}{' '}
                at spot {doc.recent.entry_spot.toFixed(2)}, model debit $
                {doc.recent.entry_debit.toFixed(2)} → settled{' '}
                <span className={doc.recent.final_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                  {usdSigned(doc.recent.final_pnl)}
                </span>
              </p>
              <TimeSeriesChart
                series={doc.recent.series}
                ariaLabel={`Simulated P&L path of the most recent ${title} analog`}
                valueFormat={usdSigned}
                timeFormat={(ts) => dateFmt.format(new Date(ts))}
              />
            </>
          )}
          {doc.assumptions && (
            <details style={{ marginTop: 10 }}>
              <summary className="muted">assumptions (read before trusting a number)</summary>
              <dl className="kv">
                {Object.entries(doc.assumptions).map(([k, v]) => (
                  <div key={k} style={{ display: 'contents' }}>
                    <dt>{k.replace(/_/g, ' ')}</dt>
                    <dd>{typeof v === 'number' ? Number(v.toFixed(4)) : String(v ?? '—')}</dd>
                  </div>
                ))}
              </dl>
            </details>
          )}
        </>
      )}
    </section>
  )
}
