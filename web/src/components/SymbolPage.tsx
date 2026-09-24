// Symbol detail: quote header, tabbed views (price history / options /
// ideas), and Google News items. Cold caches show honest placeholders; the
// refresh button spools a forced market refresh which warms bars + news
// within a serve tick. Each tab polls its own surface on a 60 s cadence
// (PriceHistoryPanel / OptionsPanel / IdeasPanel); the legacy Daily closes
// chart now lives only in the Price tab's pre-backfill fallback path.

import { useEffect, useState } from 'react'
import { getSymbol, requestMarketRefresh } from '../lib/api'
import { ago, etTime, num2 } from '../lib/format'
import { usePoll } from '../hooks/usePoll'
import type { SymbolDetail } from '../lib/types'
import { AppShell } from './AppShell'
import { Pill } from './Pill'
import { Tabs } from './Tabs'
import { IdeasPanel } from './symbol/IdeasPanel'
import { OptionsPanel } from './symbol/OptionsPanel'
import { PriceHistoryPanel } from './symbol/PriceHistoryPanel'

const ageNote = (seconds: number | null | undefined): string =>
  seconds === null || seconds === undefined ? '' : ` · data ${ago(seconds)} old`

export function SymbolPage({ sym }: { sym: string }) {
  const poll = usePoll(() => getSymbol(sym))
  const d: SymbolDetail | null = poll.data
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    if (!refreshing) return
    const t = window.setTimeout(() => setRefreshing(false), 15_000)
    return () => window.clearTimeout(t)
  }, [refreshing])

  const refresh = async () => {
    try {
      await requestMarketRefresh([sym])
      setRefreshing(true)
      poll.refresh()
    } catch {
      // 503 (sandbox) or portal 403: the pill stays honest either way
    }
  }

  const q = d?.quote ?? null
  const mid = q && q.bid !== null && q.ask !== null ? (q.bid + q.ask) / 2 : null

  return (
    <AppShell title={sym} poll={poll}>
      <div className="pill-row" style={{ marginBottom: 16 }}>
        <a href="#/market" className="muted tap-link">
          ← Market
        </a>
        {q?.source_as_of && <Pill variant="empty">source {etTime(q.source_as_of)} ET</Pill>}
        <Pill variant="empty">delayed · CBOE/Polygon</Pill>
        <button type="button" className="chip" onClick={refresh}>
          {refreshing ? 'refreshing…' : 'Refresh data'}
        </button>
      </div>

      {poll.data === null && poll.error ? (
        <div className="card empty-state">
          <p className="muted">Cannot reach the cockpit API. {poll.error}</p>
        </div>
      ) : (
        <>
          <div className="grid tiles-grid" style={{ marginBottom: 18 }}>
            <div className="tile">
              <p>Quote (mid)</p>
              <div className="num tile-value">{mid !== null ? num2(mid) : '—'}</div>
            </div>
            <div className="tile">
              <p>Close</p>
              <div className="num tile-value">{q?.close != null ? num2(q.close) : '—'}</div>
            </div>
            <div className="tile">
              <p>Change</p>
              <div className={`num tile-value ${((q?.change_pct ?? 0) >= 0) ? 'pnl-pos' : 'pnl-neg'}`}>
                {q?.change_pct != null ? `${q.change_pct >= 0 ? '+' : ''}${q.change_pct.toFixed(2)}%` : '—'}
              </div>
            </div>
            <div className="tile">
              <p>IV30 · live</p>
              <div className="num tile-value">{q?.iv30 != null ? `${q.iv30.toFixed(1)}%` : '—'}</div>
            </div>
          </div>

          <Tabs
            tabs={[
              {
                id: 'price',
                label: 'Price',
                content: (
                  <PriceHistoryPanel
                    sym={sym}
                    bars={d?.bars ?? null}
                    barsAgeSeconds={d?.bars_age_seconds}
                  />
                ),
              },
              { id: 'options', label: 'Options', content: <OptionsPanel sym={sym} /> },
              { id: 'ideas', label: 'Ideas', content: <IdeasPanel sym={sym} /> },
            ]}
          />

          <h2 className="section-title">
            News{' '}
            <span className="muted section-sub">
              (Google News RSS · ≤12 items{ageNote(d?.news_age_seconds)})
            </span>
          </h2>
          {d?.news && d.news.length > 0 ? (
            <div className="card">
              <ul className="news-list">
                {d.news.map((item, i) => (
                  <li key={i}>
                    <a href={item.link} target="_blank" rel="noopener noreferrer">
                      {item.title}
                    </a>
                    <span className="muted" style={{ display: 'block', fontSize: '0.8rem' }}>
                      {item.source && `${item.source} · `}
                      {item.pub ?? ''}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="card empty-state">
              <p className="muted">No news cached yet — hit “Refresh data”.</p>
            </div>
          )}
        </>
      )}
    </AppShell>
  )
}
