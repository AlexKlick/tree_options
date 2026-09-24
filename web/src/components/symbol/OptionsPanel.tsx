// Options tab of the symbol page (Phase 5a viewer upgrade): the desk's
// RECORDED surface — features cards as metric tiles, the ATM chain slice
// (call | strike | put) and the ATM term structure — plus the long IV30
// history. When the discovery lane's live envelope warms, a source toggle
// swaps the slice table onto it; until then a muted hint says so. Every
// degrade is honest: no recorded chains, cards-only sessions, thin rank
// histories, and withheld earnings moves all render their reason.

import { useState } from 'react'
import { getSymbolOptions } from '../../lib/api'
import { usePoll } from '../../hooks/usePoll'
import { ago, compactCount, num2 } from '../../lib/format'
import type {
  HistorySeries,
  Iv30History,
  OptionsCards,
  OptionsEarnings,
  OptionsSliceRow,
  SymbolOptions,
} from '../../lib/types'
import { Pill } from '../Pill'
import { TableScroll } from '../TableScroll'
import { TimeSeriesChart } from '../TimeSeriesChart'

/** Decimal vol (0.253) as a percent with one decimal (25.3%). */
const volPct = (v: number): string => `${(v * 100).toFixed(1)}%`

/** Signed variant for slopes and event moves. */
const volPctSigned = (v: number): string => `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`

const strikeFmt = (v: number): string =>
  `${v % 1 === 0 ? v.toFixed(0) : v.toFixed(1)}`

const nz = (v: number | null | undefined, fmt: (n: number) => string): string =>
  v === null || v === undefined ? '—' : fmt(v)

const deltaFmt = (v: number): string => `${v >= 0 ? '+' : ''}${v.toFixed(2)}`

const etDate = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  month: 'short',
  day: 'numeric',
  year: 'numeric',
})

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="card tile">
      <p className="tile-label">{label}</p>
      <div className="tile-value num">{value}</div>
    </div>
  )
}

// ---------------------------------------------------------------- slice

interface StrikeRow {
  strike: number
  atm: boolean
  call: OptionsSliceRow | null
  put: OptionsSliceRow | null
}

interface ExpiryGroup {
  exp: string
  dte: number
  strikes: StrikeRow[]
}

/** Pair calls/puts per strike, grouped per expiry, strikes ascending. */
function groupSlice(rows: OptionsSliceRow[]): ExpiryGroup[] {
  const byExp = new Map<string, Map<number, StrikeRow>>()
  const dtes = new Map<string, number>()
  for (const r of rows) {
    let strikes = byExp.get(r.exp)
    if (!strikes) {
      strikes = new Map()
      byExp.set(r.exp, strikes)
      dtes.set(r.exp, r.dte)
    }
    const row = strikes.get(r.strike) ?? {
      strike: r.strike,
      atm: false,
      call: null,
      put: null,
    }
    if (r.right === 'C') row.call = r
    else row.put = r
    row.atm = row.atm || r.atm
    strikes.set(r.strike, row)
  }
  return [...byExp.entries()]
    .map(([exp, strikes]) => ({
      exp,
      dte: dtes.get(exp) ?? 0,
      strikes: [...strikes.values()].sort((a, b) => a.strike - b.strike),
    }))
    .sort((a, b) => a.exp.localeCompare(b.exp))
}

function LegCells({ leg }: { leg: OptionsSliceRow | null }) {
  if (!leg) {
    return (
      <>
        <td className="num">—</td>
        <td className="num">—</td>
        <td className="num">—</td>
        <td className="num">—</td>
        <td className="num">—</td>
        <td className="num">—</td>
      </>
    )
  }
  return (
    <>
      <td className="num">{nz(leg.bid, num2)}</td>
      <td className="num">{nz(leg.ask, num2)}</td>
      <td className="num">{nz(leg.iv, volPct)}</td>
      <td className="num">{nz(leg.delta, deltaFmt)}</td>
      <td className="num">{nz(leg.oi, compactCount)}</td>
      <td className="num">{nz(leg.volume, compactCount)}</td>
    </>
  )
}

function SliceTable({ rows }: { rows: OptionsSliceRow[] }) {
  const groups = groupSlice(rows)
  return (
    <TableScroll>
      <table>
        <thead>
          <tr>
            <th colSpan={6}>Call</th>
            <th className="num">Strike</th>
            <th colSpan={6}>Put</th>
          </tr>
          <tr>
            <th className="num">Bid</th>
            <th className="num">Ask</th>
            <th className="num">IV</th>
            <th className="num">Delta</th>
            <th className="num">OI</th>
            <th className="num">Vol</th>
            <th className="num">$</th>
            <th className="num">Bid</th>
            <th className="num">Ask</th>
            <th className="num">IV</th>
            <th className="num">Delta</th>
            <th className="num">OI</th>
            <th className="num">Vol</th>
          </tr>
        </thead>
        <tbody>
          {groups.map((g) => (
            <ExpiryRows key={g.exp} group={g} />
          ))}
        </tbody>
      </table>
    </TableScroll>
  )
}

function ExpiryRows({ group }: { group: ExpiryGroup }) {
  return (
    <>
      <tr className="total-row">
        <td colSpan={13}>
          <span className="muted">
            {group.exp} · {group.dte}d to expiry
          </span>
        </td>
      </tr>
      {group.strikes.map((s) => (
        <tr key={`${group.exp}:${s.strike}`}>
          <LegCells leg={s.call} />
          <td className="num strike" style={s.atm ? { color: 'var(--accent)' } : undefined}>
            {strikeFmt(s.strike)}
            {s.atm ? ' · atm' : ''}
          </td>
          <LegCells leg={s.put} />
        </tr>
      ))}
    </>
  )
}

// ---------------------------------------------------------------- panel

export function OptionsPanel({ sym }: { sym: string }) {
  const poll = usePoll(() => getSymbolOptions(sym), 60_000)
  const [source, setSource] = useState<'recorded' | 'live'>('recorded')
  const o: SymbolOptions | null = poll.data

  if (o === null) {
    return (
      <div className="card empty-state">
        <p className="muted">
          {poll.error
            ? `Cannot reach the cockpit API. ${poll.error}`
            : 'Loading options surface…'}
        </p>
      </div>
    )
  }

  const live = o.live
  const ivh = o.iv30_history

  if (!o.available || o.recorded === null) {
    return (
      <>
        <div className="card empty-state">
          <p className="muted">no recorded option chains for {sym} yet</p>
        </div>
        {ivh && <Iv30HistoryCard ivh={ivh} />}
      </>
    )
  }

  const r = o.recorded
  const rank = r.cards.iv_rank
  const earnings = r.cards.earnings
  const sliceRows = source === 'live' && live ? live.slice : r.slice
  const recordedAge =
    r.age_seconds === null || r.age_seconds === undefined
      ? ''
      : ` · data ${ago(r.age_seconds)} old`
  const sliceNote =
    source === 'live' && live
      ? `live · delayed CBOE · fetched ${ago(live.age_seconds ?? 0)} ago` +
        (live.ttl_seconds !== null && live.ttl_seconds !== undefined
          ? ` · ttl ${live.ttl_seconds}s`
          : '')
      : `recorded${recordedAge} · ±5 rungs around ATM · nearest 6 expiries`

  return (
    <>
      <div className="pill-row" style={{ marginBottom: 10 }}>
        <Pill variant="empty">
          {live
            ? `recorded session ${r.session}`
            : `recorded session ${r.session} · not live`}
        </Pill>
        {live ? (
          <Pill variant="empty">
            live · delayed CBOE · {ago(live.age_seconds ?? 0)} old
          </Pill>
        ) : (
          <span className="muted" style={{ fontSize: '0.82rem' }}>
            live chain not warmed yet — Refresh data spools the warm
          </span>
        )}
      </div>

      {o.warnings.map((w, i) => (
        <p key={i} className="table-note muted">
          {w}
        </p>
      ))}

      <MetricTiles cards={r.cards} />

      {rank && !rank.status && (
        <p className="table-note muted">
          iv rank: percentile of IV30 over {rank.n ?? '?'} ranked sessions
        </p>
      )}
      {rank?.status && (
        <p className="table-note muted">
          iv rank not evaluable — {rank.reason ?? rank.status}
        </p>
      )}
      {(rank?.low_n || rank?.outside_range) && (
        <div className="pill-row" style={{ margin: '10px 0' }}>
          {rank?.low_n && (
            <Pill variant="disarmed">
              iv rank n={rank.n ?? '?'} &lt; 120 — thin history
            </Pill>
          )}
          {rank?.outside_range && (
            <Pill variant="disarmed">iv {rank.outside_range} the ranked range</Pill>
          )}
        </div>
      )}

      {earnings && <EarningsNote earnings={earnings} />}

      <h2 className="section-title">
        ATM slice{' '}
        <span className="muted section-sub">{sliceNote}</span>
      </h2>
      <div className="chip-row" role="group" aria-label="Slice source">
        <button
          type="button"
          className="chip"
          aria-pressed={source === 'recorded'}
          onClick={() => setSource('recorded')}
        >
          Recorded {r.session}
        </button>
        <button
          type="button"
          className={`chip${live ? '' : ' chip-alt'}`}
          aria-pressed={source === 'live'}
          disabled={!live}
          onClick={() => live && setSource('live')}
        >
          Live{live ? ` · ${ago(live.age_seconds ?? 0)} old` : ' · not warmed'}
        </button>
      </div>
      {sliceRows && sliceRows.length > 0 ? (
        <div className="card table-card">
          <SliceTable rows={sliceRows} />
        </div>
      ) : (
        <div className="card empty-state">
          <p className="muted">
            {source === 'live'
              ? 'no live slice rows in the envelope yet'
              : `cards only — no recorded chain slice for ${r.session}`}
          </p>
        </div>
      )}

      <TermStructureCard sym={sym} term={r.atm_term} session={r.session} />

      {ivh && <Iv30HistoryCard ivh={ivh} />}
    </>
  )
}

// ------------------------------------------------------------- subcards

function MetricTiles({ cards }: { cards: OptionsCards }) {
  const iv = cards.iv ?? {}
  const skew = cards.skew25 ?? {}
  const rank = cards.iv_rank
  const earnings = cards.earnings
  const pct = (v: number): string => `${Math.round(v * 100)}%`
  return (
    <div className="tiles">
      {(['30', '60', '90', '180'] as const).map((d) => (
        <Tile key={d} label={`IV${d}`} value={nz(iv[d], volPct)} />
      ))}
      <Tile
        label="IV rank percentile"
        value={
          rank && rank.percentile != null && rank.status === undefined
            ? pct(rank.percentile)
            : '—'
        }
      />
      <Tile label="Skew 25d · 30d" value={nz(skew['30'], volPctSigned)} />
      <Tile label="Skew 25d · 90d" value={nz(skew['90'], volPctSigned)} />
      <Tile label="Term slope (90/30)" value={nz(cards.term_slope, volPctSigned)} />
      <Tile label="YZ RV 22d" value={nz(cards.yz22_ann, volPct)} />
      <Tile label="Liquidity score" value={nz(cards.liquidity_score, String)} />
      <Tile label="Next report" value={earnings?.next_report ?? '—'} />
    </div>
  )
}

function EarningsNote({ earnings }: { earnings: OptionsEarnings }) {
  const parts: string[] = []
  if (earnings.next_report === null || earnings.next_report === undefined) {
    parts.push(earnings.reason ?? 'no known report ahead (schedule incomplete)')
  } else {
    if (earnings.implied_move != null && earnings.hist_mean_abs_move != null) {
      parts.push(
        `implied ±${volPct(earnings.implied_move)} vs hist |move| ` +
          `${volPct(earnings.hist_mean_abs_move)}` +
          (earnings.hist_n !== undefined ? ` (n=${earnings.hist_n})` : ''),
      )
    } else if (earnings.implied_move == null) {
      parts.push(
        `implied move withheld${earnings.reason ? ` — ${earnings.reason}` : ''}`,
      )
    }
    if (earnings.in_progress) parts.push('event in progress')
  }
  if (parts.length === 0) return null
  return <p className="table-note muted">earnings: {parts.join(' · ')}</p>
}

function TermStructureCard({
  sym,
  term,
  session,
}: {
  sym: string
  term: [string, number, number, number, string][] | null
  session: string
}) {
  const rows = term ?? []
  if (rows.length === 0) {
    return (
      <>
        <h2 className="section-title">
          ATM term <span className="muted section-sub">recorded {session}</span>
        </h2>
        <div className="card empty-state">
          <p className="muted">no ATM term points recorded for {session}</p>
        </div>
      </>
    )
  }
  const points = rows.map(([, dte, iv]) => [dte, iv] as [number, number])
  const ivs = points.map(([, v]) => v)
  const series: HistorySeries = {
    points,
    y_lo: Math.min(...ivs),
    y_hi: Math.max(...ivs),
    last: {
      ts_ms: points[points.length - 1][0],
      value: ivs[ivs.length - 1],
      pos: true,
    },
  }
  return (
    <>
      <h2 className="section-title">
        ATM term{' '}
        <span className="muted section-sub">
          recorded {session} · ATM IV by days-to-expiry
        </span>
      </h2>
      <div className="card chart-card">
        <TimeSeriesChart
          series={series}
          ariaLabel={`ATM term structure for ${sym}`}
          valueFormat={volPct}
          timeFormat={(v) => `${Math.round(v)}d`}
        />
      </div>
      <p className="table-note muted">
        {rows
          .map(([exp, dte, , n, how]) => `${exp} ${dte}d · ${n} strikes (${how})`)
          .join(' · ')}
      </p>
    </>
  )
}

function Iv30HistoryCard({ ivh }: { ivh: Iv30History }) {
  const lastPt = ivh.points[ivh.points.length - 1]
  const series: HistorySeries = {
    points: ivh.points,
    y_lo: ivh.y_lo,
    y_hi: ivh.y_hi,
    last: { ts_ms: lastPt[0], value: lastPt[1], pos: true },
  }
  return (
    <>
      <h2 className="section-title">
        IV30 history{' '}
        <span className="muted section-sub">
          {ivh.first}..{ivh.last} · {ivh.n} sessions · {ivh.source}
        </span>
      </h2>
      <div className="card chart-card">
        <TimeSeriesChart
          series={series}
          ariaLabel="IV30 history"
          valueFormat={volPct}
          timeFormat={(ts) => etDate.format(new Date(ts))}
        />
      </div>
      <p className="table-note muted">
        the IV history is a manual build whose last session ({ivh.last}) can
        trail the chain store — the extent is disclosed, not hidden
      </p>
    </>
  )
}
