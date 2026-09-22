import { getDiscovery, requestScan } from '../lib/api'
import { usePoll } from '../hooks/usePoll'
import { useDensity } from '../density'
import { etTime } from '../lib/format'
import type { CandidateRow } from '../lib/types'
import { AppShell } from './AppShell'
import { CandidateTable } from './CandidateTable'
import { ShadowSection } from './ShadowSection'
import { Pill } from './Pill'

function agePill(seconds: number | null): JSX.Element {
  if (seconds === null) return <Pill variant="empty">○ never scanned</Pill>
  if (seconds > 3600)
    return <Pill variant="disarmed">● scan {Math.round(seconds / 3600)}h ago</Pill>
  if (seconds > 600) return <Pill variant="empty">○ scan {Math.round(seconds / 60)}m ago</Pill>
  return <Pill variant="armed">● scan {seconds}s ago</Pill>
}

function SimpleCards({ rows }: { rows: CandidateRow[] }) {
  return (
    <div className="grid">
      {rows.slice(0, 4).map((c, i) => (
        <div className="card" key={i}>
          <div className="card-head">
            <h3>
              {c.underlying} {c.long_strike}/{c.short_strike} put spread
            </h3>
            <span className="muted num">#{c.rank}</span>
          </div>
          <dl className="kv">
            <dt>Debit</dt>
            <dd className="num">{c.debit_mid !== null ? `$${c.debit_mid.toFixed(2)}` : '—'}</dd>
            <dt>Max profit</dt>
            <dd className="num pnl-pos">
              {c.max_profit !== null ? `$${Math.round(c.max_profit).toLocaleString()}` : '—'}
            </dd>
            <dt>Yield</dt>
            <dd className="num strike">
              {c.yield_ratio !== null ? `${c.yield_ratio.toFixed(1)}:1` : '—'}
            </dd>
            <dt>Expires</dt>
            <dd className="num">
              {c.expiry} ({c.dte}d)
            </dd>
          </dl>
        </div>
      ))}
    </div>
  )
}

export function DiscoverPage() {
  const poll = usePoll(getDiscovery)
  const { mode: density } = useDensity()
  const d = poll.data
  const latest = d?.latest ?? null

  const runScan = async () => {
    try {
      await requestScan()
    } catch {
      // 503 (sandboxed spool) or portal 403: refresh shows pending state
    }
    poll.refresh()
  }

  return (
    <AppShell title="Discover" poll={poll}>
      <div className="pill-row" style={{ marginBottom: 16 }}>
        {agePill(latest?.age_seconds ?? null)}
        {latest && (
          <Pill variant={latest.data_quality.chains_available ? 'armed' : 'disarmed'}>
            {latest.data_quality.chains_available
              ? `● ${latest.data_quality.underlyings_scanned}/${latest.data_quality.underlyings_requested} scanned`
              : '○ no chain data'}
          </Pill>
        )}
        {d?.spool.pending && <Pill variant="empty">○ scan requested — runner pending</Pill>}
        {latest && !latest.data_quality.greeks_available && (
          <Pill variant="empty">○ delayed feed: no greeks (premium targeting)</Pill>
        )}
        <button type="button" className="chip" onClick={runScan}>
          Run scan
        </button>
      </div>

      {latest === null ? (
        <div className="card empty-state">
          <div className="icon">◎</div>
          <h2>No scan yet</h2>
          <p className="muted">
            Hit &ldquo;Run scan&rdquo; — the discovery runner picks the request up
            from its spool and the results appear here.
          </p>
        </div>
      ) : (
        <>
          <p className="muted" style={{ marginTop: 0 }}>
            ranked put-debit spreads · target{' '}
            {latest.effective_target_modes.join('/') || '?'} · generated{' '}
            {latest.generated_at ? etTime(latest.generated_at) : '?'} ET
            {latest.data_quality.rows_unquoted > 0 &&
              ` · ${latest.data_quality.rows_unquoted} rows unquoted`}
          </p>
          {latest.candidates.length === 0 ? (
            <div className="card">
              <p className="muted" style={{ margin: 0 }}>
                No candidates passed the rules this scan.
              </p>
            </div>
          ) : density === 'simple' ? (
            <SimpleCards rows={latest.candidates} />
          ) : (
            <CandidateTable rows={latest.candidates} />
          )}
          {latest.rejected.length > 0 && density !== 'simple' && (
            <>
              <h2 className="section-title">
                Rejected <span className="muted section-sub">(rule audit disclosed)</span>
              </h2>
              <CandidateTable rows={latest.rejected} />
            </>
          )}
          <ShadowSection shadow={d?.shadow ?? null} />
        </>
      )}
      {!latest && <ShadowSection shadow={d?.shadow ?? null} />}
    </AppShell>
  )
}
