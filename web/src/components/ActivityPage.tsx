// Activity & settings (#/activity, #/activity/acceptance, #/activity/settings).
//
// - Activity: the paged audit feed of GET /api/activity (trex-activity/1),
//   newest first, filtered by source on the server, with who acted and
//   through which door (Family Apps, loopback, the desk itself). A source
//   the server cannot read is said out loud, never shown as an empty log.
// - Paper acceptance: GET /api/acceptance (trex-acceptance-view/1), progress
//   against every requirement. Acceptance never unlocks live trading.
// - Settings: the read-only SettingsPanel.

import { useCallback, useEffect, useRef, useState } from 'react'
import { usePoll, type PollState } from '../hooks/usePoll'
import {
  ACTIVITY_PAGE_SIZE,
  COMMAND_STATUS_LABELS,
  DRILL_LABELS,
  SOURCE_LABELS,
  actorText,
  appendPage,
  getAcceptance,
  getActivity,
  isProposalId,
  reasonText,
  toggleSource,
} from '../lib/activityApi'
import {
  ACTIVITY_SOURCES,
  type AcceptanceDocument,
  type ActivityItem,
  type ActivityPage as ActivityPageView,
  type ActivityRefKey,
  type ActivitySource,
  type DrillId,
  type SessionMetric,
  type SessionRecord,
} from '../lib/contracts'
import { etDateTime } from '../lib/format'
import { hashFor } from '../lib/routes'
import { AppShell } from './AppShell'
import { Section, StatusPill, Unavailable, type Tone } from './PortfolioPage'
import { SettingsPanel } from './SettingsPanel'
import './accountPages.css'

export type ActivitySection = 'feed' | 'acceptance' | 'settings'

/** The section a routes.ts route id opens (`activity`, `activity-acceptance`,
 * `activity-settings`); anything else opens the feed. */
export function activitySectionFor(routeId: string): ActivitySection {
  if (routeId === 'activity-acceptance') return 'acceptance'
  if (routeId === 'activity-settings') return 'settings'
  return 'feed'
}

const SECTIONS: { id: ActivitySection; label: string; hash: string }[] = [
  { id: 'feed', label: 'Activity', hash: hashFor('activity') },
  { id: 'acceptance', label: 'Paper acceptance', hash: hashFor('activity-acceptance') },
  { id: 'settings', label: 'Settings', hash: hashFor('activity-settings') },
]

const TITLE = 'Activity & settings'
const LIVE_NOTE =
  'Paper acceptance never unlocks live trading: live trading would need separately configured limits, account qualification and an explicit activation.'

function SubNav({ section }: { section: ActivitySection }) {
  return (
    <nav aria-label={TITLE} className="acct-subnav">
      {SECTIONS.map((s) => (
        <a key={s.id} href={s.hash} aria-current={s.id === section ? 'page' : undefined}>
          {s.label}
        </a>
      ))}
    </nav>
  )
}

// ---------------------------------------------------------------- the feed

/** Refresh the newest page this often, only while no older page is loaded
 * (refreshing the head under loaded older pages could open a gap). */
const FEED_REFRESH_MS = 30_000

interface FeedState {
  items: ActivityItem[]
  last: ActivityPageView | null
  error: string | null
  loading: boolean
  lastSuccessAt: number | null
  pages: number
}
const EMPTY_FEED: FeedState = { items: [], last: null, error: null, loading: true, lastSuccessAt: null, pages: 0 }

function useActivityFeed(sources: ActivitySource[]) {
  const [state, setState] = useState<FeedState>(EMPTY_FEED)
  const generation = useRef(0)
  const sourcesRef = useRef(sources)
  sourcesRef.current = sources
  const key = sources.join(',')

  // Every load starts a new generation; only the latest may commit, so a
  // filter change or a refresh supersedes whatever was in flight.
  const load = useCallback((cursor: string | null) => {
    const gen = ++generation.current
    setState((s) => ({ ...s, loading: true }))
    getActivity({ limit: ACTIVITY_PAGE_SIZE, cursor, sources: sourcesRef.current }).then(
      (page) => {
        if (gen !== generation.current) return
        setState((s) => ({
          items: cursor === null ? page.items : appendPage(s.items, page),
          last: page,
          error: null,
          loading: false,
          lastSuccessAt: Date.now(),
          pages: cursor === null ? 1 : s.pages + 1,
        }))
      },
      (err: unknown) => {
        if (gen !== generation.current) return
        setState((s) => ({ ...s, error: err instanceof Error ? err.message : String(err), loading: false }))
      },
    )
  }, [])

  useEffect(() => {
    setState(EMPTY_FEED)
    load(null)
    return () => {
      generation.current += 1
    }
  }, [key, load])

  useEffect(() => {
    if (state.pages > 1) return
    const timer = window.setInterval(() => {
      if (!document.hidden) load(null)
    }, FEED_REFRESH_MS)
    return () => window.clearInterval(timer)
  }, [state.pages, load])

  const poll: PollState<unknown> = {
    data: state.last,
    error: state.error,
    lastSuccessAt: state.lastSuccessAt,
    isStale: false,
    refresh: () => load(null),
  }
  return { ...state, poll, refresh: () => load(null), loadOlder: () => load(state.last?.next_cursor ?? null) }
}

const STATUS_TONE: Record<NonNullable<ActivityItem['command_status']>, Tone> = {
  accepted: 'unknown',
  applied: 'ok',
  refused: 'bad',
  failed: 'bad',
  duplicate: 'warn',
}

const REF_LABELS: Record<ActivityRefKey, string> = {
  proposal_id: 'Proposal',
  intent_id: 'Intent',
  program_id: 'Program',
  drill_id: 'Drill',
  path: 'File',
}

function RefLine({ refKey, value }: { refKey: ActivityRefKey; value: string }) {
  if (refKey === 'proposal_id' && isProposalId(value)) {
    return (
      <li>
        {REF_LABELS[refKey]} <code>{value.slice(0, 12)}</code>{' '}
        <a href={hashFor('trade-review-proposal', value)}>Open the proposal</a>
      </li>
    )
  }
  if (refKey === 'drill_id' && value in DRILL_LABELS) {
    return (
      <li>
        {REF_LABELS[refKey]}: <a href={hashFor('activity-acceptance')}>{DRILL_LABELS[value as DrillId]}</a>
      </li>
    )
  }
  return (
    <li>
      {REF_LABELS[refKey]}: <code>{value}</code>
    </li>
  )
}

function FeedItem({ item }: { item: ActivityItem }) {
  const refs = Object.entries(item.refs) as [ActivityRefKey, string][]
  return (
    <li className="acct-row">
      <article aria-label={item.summary}>
        <div className="acct-row-head">
          <time dateTime={item.at}>{etDateTime(item.at)}</time>
          <span className="muted">{SOURCE_LABELS[item.source]}</span>
          <span className="muted">{item.kind.replace(/_/g, ' ')}</span>
          {item.command_status !== null && (
            <StatusPill tone={STATUS_TONE[item.command_status]}>{COMMAND_STATUS_LABELS[item.command_status]}</StatusPill>
          )}
        </div>
        <p>{item.summary}</p>
        <p className="acct-row-meta">{actorText(item)}</p>
        {item.reason !== null && <p className="acct-row-meta">Reason: {reasonText(item.reason)}</p>}
        {refs.length > 0 && (
          <ul className="acct-row-meta">
            {refs.map(([refKey, value]) => (
              <RefLine key={refKey} refKey={refKey} value={value} />
            ))}
          </ul>
        )}
      </article>
    </li>
  )
}

function SourceFilter({ sources, onChange }: { sources: ActivitySource[]; onChange: (next: ActivitySource[]) => void }) {
  return (
    <div className="acct-actions" role="group" aria-label="Show sources">
      <button type="button" aria-pressed={sources.length === 0} onClick={() => onChange([])}>
        All sources
      </button>
      {ACTIVITY_SOURCES.map((source) => (
        <button
          key={source}
          type="button"
          aria-pressed={sources.includes(source)}
          onClick={() => onChange(toggleSource(sources, source))}
        >
          {SOURCE_LABELS[source]}
        </button>
      ))}
    </div>
  )
}

function SourcesStatus({ page }: { page: ActivityPageView }) {
  return (
    <Section title="Sources" id="activity-sources">
      <ul>
        {page.sources.map((s) => (
          <li key={s.source}>
            {SOURCE_LABELS[s.source]}:{' '}
            {s.status === 'ok' ? (
              'readable'
            ) : (
              <>
                unavailable: {reasonText(s.unavailable_reason)}. Its items are missing below; this is not an empty
                log.
              </>
            )}
          </li>
        ))}
      </ul>
    </Section>
  )
}

function FeedScreen() {
  const [sources, setSources] = useState<ActivitySource[]>([])
  const feed = useActivityFeed(sources)
  const someUnavailable = feed.last?.sources.some((s) => s.status === 'unavailable') ?? false
  return (
    <AppShell
      title={TITLE}
      poll={feed.poll}
      showRuntimeBanners={false}
      contextLabel="trex · activity"
      footerSource="Read-only: commands, desk events, audit records and drills."
    >
      <div className="acct-page">
        <SubNav section="feed" />
        <p className="acct-intro">
          What you and TREX did, newest first: commands from the phone or this computer, desk events, audit records and
          drills.
        </p>
        <SourceFilter sources={sources} onChange={setSources} />
        {feed.error && (
          <p role="alert" className="acct-alert">
            Activity could not be read: {feed.error}.{feed.items.length > 0 ? ' Showing what was received before.' : ''}
          </p>
        )}
        {feed.last === null && !feed.error && <p role="status">Loading activity…</p>}
        {feed.last && <SourcesStatus page={feed.last} />}
        {feed.last && feed.items.length === 0 && (
          <p>{someUnavailable ? 'No activity from the readable sources.' : 'No activity recorded yet.'}</p>
        )}
        {feed.items.length > 0 && (
          <section className="card" aria-label="Activity log">
            <ol className="acct-list">
              {feed.items.map((item) => (
                <FeedItem key={item.id} item={item} />
              ))}
            </ol>
          </section>
        )}
        <div className="acct-actions">
          {feed.last?.has_more && (
            <button type="button" onClick={feed.loadOlder} disabled={feed.loading}>
              Load older activity
            </button>
          )}
          {feed.last && !feed.last.has_more && feed.items.length > 0 && (
            <span className="muted">Start of the activity log.</span>
          )}
          <button type="button" onClick={feed.refresh} disabled={feed.loading}>
            Refresh
          </button>
        </div>
      </div>
    </AppShell>
  )
}

// ---------------------------------------------------------------- acceptance

const METRIC_LABELS: Record<SessionMetric, string> = {
  heartbeat_minutes: 'Heartbeat',
  reconciliation_open: 'Books at the open',
  reconciliation_close: 'Books at the close',
  reconciliation_episodes: 'Mismatch episodes',
  unowned_orders: 'Orders TREX does not own',
  max_exit_unguarded_s: 'Longest unwatched exit',
}

function regularMinutes(doc: AcceptanceDocument, session: string): number | null {
  const window = doc.calendar_window.find((w) => w.session === session)
  if (!window) return null
  return Math.round((Date.parse(window.closes_at) - Date.parse(window.opens_at)) / 60_000)
}

function metricText(doc: AcceptanceDocument, record: SessionRecord, metric: SessionMetric): string {
  const missing = record.unavailable[metric]
  if (record[metric] === null) return `unavailable: ${reasonText(missing ?? 'not_recorded')}`
  switch (metric) {
    case 'heartbeat_minutes': {
      const of = regularMinutes(doc, record.session)
      return `${record.heartbeat_minutes} of ${of ?? '?'} minutes fresh`
    }
    case 'reconciliation_open':
    case 'reconciliation_close':
      return String(record[metric]).replace(/_/g, ' ')
    case 'reconciliation_episodes': {
      const episodes = record.reconciliation_episodes ?? []
      const open = episodes.filter((e) => e.resolved_at === null).length
      return `${episodes.length} (${open} unresolved)`
    }
    case 'unowned_orders':
      return String(record.unowned_orders)
    case 'max_exit_unguarded_s':
      return `${record.max_exit_unguarded_s} s`
  }
}

function ProgressSection({ doc }: { doc: AcceptanceDocument }) {
  const met = doc.verdict === 'met'
  return (
    <Section
      title="Progress"
      id="acceptance-progress"
      status={<StatusPill tone={met ? 'ok' : 'warn'}>{met ? 'Met' : 'Not met yet'}</StatusPill>}
    >
      <dl className="kv">
        <dt>Qualifying sessions in a row</dt>
        <dd>
          <span className="acct-meter">{`${doc.streak.current_sessions} of ${doc.streak.required}`}</span>{' '}
          <span className="muted">
            {doc.streak.current_start && doc.streak.current_end
              ? `(${doc.streak.current_start} to ${doc.streak.current_end}; best ${doc.streak.best_sessions})`
              : `(best ${doc.streak.best_sessions})`}
          </span>
        </dd>
        <dt>Option paper trades counted</dt>
        <dd>
          <span className="acct-meter">{`${doc.per_kind.option.counted} of ${doc.per_kind.option.required}`}</span>{' '}
          <span className="muted">({doc.per_kind.option.completed_total} completed in all)</span>
        </dd>
        <dt>Stock paper trades counted</dt>
        <dd>
          <span className="acct-meter">{`${doc.per_kind.equity.counted} of ${doc.per_kind.equity.required}`}</span>{' '}
          <span className="muted">({doc.per_kind.equity.completed_total} completed in all)</span>
        </dd>
        <dt>Drills passed</dt>
        <dd>
          <span className="acct-meter">{`${doc.drills_passed} of ${doc.drills_required}`}</span>
        </dd>
        <dt>Computed</dt>
        <dd>{etDateTime(doc.computed_at)}</dd>
      </dl>
      {doc.verdict_reasons.length > 0 && (
        <>
          <h3>Still missing</h3>
          <ul>
            {doc.verdict_reasons.map((reason) => (
              <li key={reason}>{reasonText(reason)}</li>
            ))}
          </ul>
        </>
      )}
      <p className="muted">
        Only trades approved and closed inside the current streak count. {LIVE_NOTE}
      </p>
    </Section>
  )
}

function SessionsSection({ doc }: { doc: AcceptanceDocument }) {
  const records = new Map(doc.sessions.map((s) => [s.session, s]))
  const metrics = Object.keys(METRIC_LABELS) as SessionMetric[]
  return (
    <Section title="Sessions" id="acceptance-sessions">
      <ul className="acct-list">
        {[...doc.session_results].reverse().map((result) => {
          const record = records.get(result.session)
          const [tone, word]: [Tone, string] = !result.recorded
            ? ['unknown', 'Not recorded']
            : result.qualifying
              ? ['ok', 'Qualifying']
              : ['bad', 'Not qualifying']
          return (
            <li className="acct-row" key={result.session}>
              <div className="acct-row-head">
                <strong>{result.session}</strong>
                <StatusPill tone={tone}>{word}</StatusPill>
              </div>
              {result.reasons.length > 0 && (
                <ul>
                  {result.reasons.map((reason) => (
                    <li key={reason}>{reasonText(reason)}</li>
                  ))}
                </ul>
              )}
              {record && (
                <p className="acct-row-meta">
                  {metrics.map((metric) => `${METRIC_LABELS[metric]}: ${metricText(doc, record, metric)}`).join(' · ')}
                </p>
              )}
            </li>
          )
        })}
      </ul>
    </Section>
  )
}

function LifecyclesSection({ doc }: { doc: AcceptanceDocument }) {
  const records = new Map(doc.lifecycles.map((l) => [l.intent_id, l]))
  return (
    <Section title="Paper trades" id="acceptance-lifecycles">
      {doc.lifecycle_results.length === 0 ? (
        <p>No approved paper trade is recorded yet.</p>
      ) : (
        <ul className="acct-list">
          {doc.lifecycle_results.map((result) => {
            const record = records.get(result.intent_id)
            const [tone, word]: [Tone, string] = result.counted
              ? ['ok', 'Counted']
              : result.completed
                ? ['warn', 'Completed, outside the streak']
                : ['unknown', 'Not completed']
            return (
              <li className="acct-row" key={result.intent_id}>
                <div className="acct-row-head">
                  <strong>{result.intent_id}</strong>
                  <span>{result.asset_kind === 'option' ? 'Option' : 'Stock'}</span>
                  <StatusPill tone={tone}>{word}</StatusPill>
                </div>
                {record && (
                  <p className="acct-row-meta">
                    Approved {etDateTime(record.approved_at)} · filled {record.filled_quantity} · exit{' '}
                    {record.exit_by.replace(/_/g, ' ')}
                    {record.exited_at ? ` ${etDateTime(record.exited_at)}` : ''}
                  </p>
                )}
                {result.reasons.length > 0 && (
                  <ul>
                    {result.reasons.map((reason) => (
                      <li key={reason}>{reasonText(reason)}</li>
                    ))}
                  </ul>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </Section>
  )
}

const DRILL_STATUS: Record<'not_run' | 'passed' | 'failed', [Tone, string]> = {
  passed: ['ok', 'Passed'],
  failed: ['bad', 'Failed'],
  not_run: ['unknown', 'Not run'],
}

function DrillsSection({ doc }: { doc: AcceptanceDocument }) {
  const records = new Map(doc.drills.map((d) => [d.id, d]))
  return (
    <Section title="Drills" id="acceptance-drills">
      <ul className="acct-list">
        {doc.drill_results.map((result) => {
          const record = records.get(result.id)
          const [tone, word] = DRILL_STATUS[record?.status ?? 'not_run']
          return (
            <li className="acct-row" key={result.id}>
              <div className="acct-row-head">
                <strong>{DRILL_LABELS[result.id]}</strong>
                <StatusPill tone={result.passed ? 'ok' : tone}>{result.passed ? 'Passed' : word}</StatusPill>
              </div>
              {record && record.at && (
                <p className="acct-row-meta">
                  {etDateTime(record.at)}
                  {record.attested_by ? `, attested by ${record.attested_by}` : ', not attested'}
                  {record.evidence.length > 0 ? `, evidence ${record.evidence.map((e) => e.path).join(', ')}` : ''}
                </p>
              )}
              {record && record.note && <p>{record.note}</p>}
              {result.reasons.length > 0 && (
                <ul className="acct-row-meta">
                  {result.reasons.map((reason) => (
                    <li key={reason}>{reasonText(reason)}</li>
                  ))}
                </ul>
              )}
            </li>
          )
        })}
      </ul>
    </Section>
  )
}

function AcceptanceScreen() {
  const poll = usePoll(getAcceptance, 60_000)
  const view = poll.data
  return (
    <AppShell
      title={TITLE}
      poll={poll}
      showRuntimeBanners={false}
      contextLabel="trex · paper acceptance"
      footerSource="Computed from the session, trade and drill records; checked again on every read."
    >
      <div className="acct-page">
        <SubNav section="acceptance" />
        <p className="acct-intro">
          Paper acceptance asks for ten qualifying sessions in a row, ten completed paper trades of each kind inside
          them, and every drill passed. It is computed from the records, never typed in.
        </p>
        {poll.error && (
          <p role="alert" className="acct-alert">
            Paper acceptance could not be read: {poll.error}.{view ? ' Showing the last record received.' : ''}
          </p>
        )}
        {!view && !poll.error && <p role="status">Loading paper acceptance…</p>}
        {view &&
          (view.status === 'unavailable' ? (
            <Section title="Progress" id="acceptance-progress" status={<StatusPill tone="unknown">Unavailable</StatusPill>}>
              <Unavailable reason={view.unavailable_reason} />
              <p className="muted">{LIVE_NOTE}</p>
            </Section>
          ) : (
            <>
              <ProgressSection doc={view.document} />
              <SessionsSection doc={view.document} />
              <LifecyclesSection doc={view.document} />
              <DrillsSection doc={view.document} />
            </>
          ))}
      </div>
    </AppShell>
  )
}

// ---------------------------------------------------------------- settings

function SettingsScreen() {
  return (
    <AppShell
      title={TITLE}
      showRuntimeBanners={false}
      contextLabel="trex · settings"
      footerSource="Read-only: settings change on the host, never from this page."
    >
      <div className="acct-page">
        <SubNav section="settings" />
        <SettingsPanel />
      </div>
    </AppShell>
  )
}

export function ActivityPage({ section = 'feed' }: { section?: ActivitySection }) {
  if (section === 'acceptance') return <AcceptanceScreen />
  if (section === 'settings') return <SettingsScreen />
  return <FeedScreen />
}
