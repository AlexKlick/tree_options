// Market desk: delayed quotes for the tracked symbols with DATA-age
// pills (source timestamps, not transport freshness). Quotes come from
// the discovery lane's market cycle (CBOE delayed, keyless).

import { useEffect, useState } from 'react'
import { getMarket, watchOp } from '../lib/api'
import { etTime, num2 } from '../lib/format'
import { usePoll } from '../hooks/usePoll'
import type { MarketQuote } from '../lib/types'
import { AppShell } from './AppShell'
import { Pill } from './Pill'

function agePill(seconds: number | null): JSX.Element {
  if (seconds === null) return <Pill variant="empty">○ no snapshot yet</Pill>
  if (seconds > 300) return <Pill variant="disarmed">● {Math.round(seconds / 60)}m old</Pill>
  return <Pill variant="armed">● {seconds}s old</Pill>
}

function QuoteCard({ sym, q }: { sym: string; q: MarketQuote }) {
  const mid = q.bid !== null && q.ask !== null ? (q.bid + q.ask) / 2 : null
  const up = (q.change_pct ?? 0) >= 0
  return (
    <a className="card quote-card" href={`#/market/${sym}`}>
      <div className="card-head">
        <h3>{sym}</h3>
        <span className={`num ${up ? 'pnl-pos' : 'pnl-neg'}`}>
          {q.change_pct !== null ? `${up ? '+' : ''}${q.change_pct.toFixed(2)}%` : '—'}
        </span>
      </div>
      <div className="num tile-value">{mid !== null ? num2(mid) : '—'}</div>
      <p className="muted" style={{ margin: 0 }}>
        {q.close !== null ? `close ${num2(q.close)}` : 'no close'}
        {q.iv30 !== null && ` · iv30 ${q.iv30.toFixed(1)}`}
      </p>
      <p className="muted" style={{ margin: 0, fontSize: '0.78rem' }}>
        {q.source_as_of ? `source ${etTime(q.source_as_of)} ET` : 'source time unknown'}
      </p>
    </a>
  )
}

export function MarketPage() {
  const poll = usePoll(getMarket)
  const d = poll.data
  const [pending, setPending] = useState('')
  const [draft, setDraft] = useState('')
  useEffect(() => {
    if (!pending) return
    const t = window.setTimeout(() => setPending(''), 15_000)
    return () => window.clearTimeout(t)
  }, [pending])

  const submitOp = async (op: 'add' | 'remove', sym: string) => {
    try {
      await watchOp(op, sym)
      setPending(sym.toUpperCase())
      poll.refresh()
    } catch {
      /* 503 or portal 403: the next poll reveals the truth */
    }
  }

  const addDraft = () => {
    const sym = draft.trim().toUpperCase()
    if (!/^[A-Z.]{1,6}$/.test(sym)) return
    setDraft('')
    void submitOp('add', sym)
  }

  const watched = new Set(d?.watchlist ?? [])
  const symbols = Object.entries(d?.symbols ?? {})
  return (
    <AppShell title="Market" poll={poll}>
      <div className="pill-row" style={{ marginBottom: 16 }}>
        {agePill(d?.age_seconds ?? null)}
        <Pill variant="empty">delayed · CBOE · keyless</Pill>
        <input
          className="chip-input"
          placeholder="add symbol (e.g. TSM)"
          value={draft}
          aria-label="Add watchlist symbol"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') addDraft()
          }}
        />
        <button type="button" className="chip" onClick={addDraft}>
          + watch
        </button>
      </div>
      {pending && (
        <p className="muted" style={{ marginTop: 0 }}>
          {pending} queued — the runner applies it within seconds.
        </p>
      )}
      {poll.data === null && poll.error ? (
        <div className="card empty-state">
          <p className="muted">Cannot reach the cockpit API. {poll.error}</p>
        </div>
      ) : symbols.length === 0 ? (
        <div className="card empty-state">
          <div className="icon">◎</div>
          <h2>No market snapshot yet</h2>
          <p className="muted">
            The discovery runner refreshes quotes every minute. If this
            stays empty, check trex-discovery.
          </p>
        </div>
      ) : (
        <div className="grid">
          {symbols.map(([sym, q]) => (
            <div key={sym} className="quote-card-wrap">
              <QuoteCard sym={sym} q={q} />
              {watched.has(sym) && (
                <button
                  type="button"
                  className="chip chip-subtle"
                  onClick={() => submitOp('remove', sym)}
                >
                  unwatch
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {d?.errors && Object.keys(d.errors).length > 0 && (
        <p className="muted" style={{ marginTop: 12 }}>
          {Object.entries(d.errors).map(([sym, err]) => `${sym}: ${err}`).join(' · ')}
        </p>
      )}
    </AppShell>
  )
}
