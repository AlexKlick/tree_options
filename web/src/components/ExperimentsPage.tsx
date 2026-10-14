import { useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import { getExperiments, type Experiment } from '../lib/experimentsApi'
import { AppShell } from './AppShell'

const labels = {historical_simulation: 'Historical simulation', forward_shadow: 'Forward shadow observation', broker_paper: 'Broker paper trading', live: 'Live trading', unavailable: 'Evidence class unavailable'}
const link = (id: string) => `#/experiments/${encodeURIComponent(id)}`

function ExperimentCard({row, expanded}: {row: Experiment; expanded: boolean}) {
  return <article className="card experiment-card" aria-labelledby={`title-${row.id}`}>
    <p className="eyebrow">{labels[row.evidence_class]}</p>
    <h2 id={`title-${row.id}`}><a href={link(row.id)}>{row.title}</a></h2>
    <p>{row.hypothesis}</p>
    <p><strong>Status:</strong> {row.status.replace(/_/g, ' ')} · <strong>Freshness:</strong> {row.freshness}</p>
    <p className="muted">{row.freshness_reason} {row.source_time && <>Source time: <time dateTime={row.source_time}>{row.source_time}</time>.</>}</p>
    {row.superseded_by && <p role="note">Superseded accounting. <a href={link(row.superseded_by)}>Read the corrected record</a>.</p>}
    {row.supersedes && <p>Post-hoc correction of <a href={link(row.supersedes)}>the preserved original record</a>. This is not another independent sample.</p>}
    <dl className="experiment-metrics">{row.metrics.map(m => <div key={m.label}><dt>{m.label}</dt><dd>{m.value === null ? `Unavailable — ${m.reason}` : `${m.value} ${m.unit}`}</dd></div>)}</dl>
    {row.blockers.length > 0 && <div><h3>Needs attention</h3><ul>{row.blockers.map(b => <li key={b}>{b}</li>)}</ul></div>}
    <p><strong>Next decision:</strong> {row.next_decision}</p>
    <details open={expanded || undefined}>
      <summary>Limitations and source details</summary>
      <ul>{row.limitations.map(l => <li key={l}>{l}</li>)}</ul>
      <p>Research evidence does not authorize trading or establish an account return.</p>
      {row.artifacts.map(a => <p key={a.label}>{a.label}: <code>{a.sha256}</code></p>)}
      <a href={row.advanced_link}>Open original research view</a>
    </details>
  </article>
}

export function ExperimentsPage({id}: {id?: string}) {
  const poll = usePoll(getExperiments)
  const [source, setSource] = useState('all')
  const [evidence, setEvidence] = useState('all')
  const data = poll.data
  const rows = data?.experiments.filter(r => id ? r.id === id :
    (source === 'all' || r.source === source) && (evidence === 'all' || r.evidence_class === evidence)) ?? []
  return <AppShell title="Experiments" poll={poll} showRuntimeBanners={false}
    contextLabel="trex · research evidence" footerSource="Original study owners retain their records and evaluation rules.">
    <div className="experiments-page">
      <p>Understand what each study established, what is missing, and its next decision.</p>
      <p>Historical simulations, forward observations, broker paper trading and live trading are separate evidence classes. Repeated replays add no new market sessions.</p>
      {poll.error && <p role="alert">{poll.error}{data && ' Last received records are shown below; current state is unavailable.'}</p>}
      {!data && !poll.error && <p role="status">Loading experiments…</p>}
      <button onClick={poll.refresh}>Refresh experiments</button>
      {id && <p><a href="#/experiments">All experiments</a></p>}
      {data && <>
        <p className="muted">Inventory published <time dateTime={data.published_at}>{data.published_at}</time>. Publication time does not refresh the underlying observations.</p>
        {data.sources.some(s => s.status === 'unavailable') && <section className="card" aria-label="Unavailable sources">
          <h2>Unavailable sources</h2><ul>{data.sources.filter(s => s.status === 'unavailable').map(s => <li key={s.id}>{s.id}: {s.reason}. Restore this source to inspect its studies.</li>)}</ul>
        </section>}
        {!id && <>
          <section className="card" aria-label="Research pilot"><h2>Three-program research pilot</h2>
            <p>The equity programs are blocked drafts, not running studies. No new requests, compute or storage are authorized by this view. Existing QSL and the nightly challenge retain their original budgets and owners.</p>
            {data.pilot.map(p => <div key={p.id}><h3>{p.title} · {p.state.replace(/_/g, ' ')}</h3><p>{p.hypothesis}</p><ul>{p.blockers.map(b => <li key={b}>{b}</li>)}</ul><p>{p.next_decision}</p></div>)}
          </section>
          <div className="experiment-filters">
            <label>Research source<select value={source} onChange={e => setSource(e.target.value)}><option value="all">All sources</option>{data.sources.map(s => <option key={s.id} value={s.id}>{s.id}</option>)}</select></label>
            <label>Evidence class<select value={evidence} onChange={e => setEvidence(e.target.value)}><option value="all">All evidence classes</option>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
          </div>
        </>}
        {rows.length === 0 && <p>{id ? 'Experiment not found in the current publication.' : 'No records in this selection. Check source availability above; this is not an empty broker portfolio.'}</p>}
        {rows.map(row => <ExperimentCard key={row.id} row={row} expanded={Boolean(id)} />)}
        <details><summary>Publication details</summary><p>Source release: <code>{data.source_release}</code></p></details>
      </>}
    </div>
  </AppShell>
}
