import { getQuantLab } from '../lib/api'
import { usePoll } from '../hooks/usePoll'
import { AppShell } from './AppShell'

export function QuantPage() {
  const poll = usePoll(getQuantLab, 15_000)
  const lab = poll.data
  return <AppShell title="Quant lab" poll={poll} showRuntimeBanners={false} footerSource="Research and broker-paper evidence">
    <h1>Strategy → Experiment → Execution</h1>
    <p>Research targets require a separate mandate and permit before execution.</p>
    {!lab && <p role="status">{poll.error ? 'Quant evidence unavailable' : 'Loading quant evidence…'}</p>}
    {lab && <>
      <section className="card" aria-label="Evidence classes">
        <p>{lab.evidence_classes.join(' · ')}</p>
        <p>Live money disabled · Broker paper: {lab.execution.state}</p>
        <p>Account: {lab.execution.account_alias ?? 'unverified'} · Owner: {lab.execution.owner_epoch ?? 'unverified'} · Mandate: {lab.execution.mandate?.state ?? 'absent'}</p>
      </section>
      <section className="card"><h2>Strategies</h2>
        {lab.strategies.map(s => <article key={s.strategy_id}><h3>{s.strategy_id}/v{s.version}</h3>
          <p>{s.registration} · {s.data_status === 'supported' ? 'Research inputs supported' : 'DATA GATED'}</p>
          <p>{s.description}</p><p>Required inputs: {s.required_inputs.join(', ')}</p>
        </article>)}
      </section>
      <section className="card"><h2>Experiments and provenance</h2>
        {lab.experiments.length === 0 && <p>No persisted quant experiment.</p>}
        {lab.experiments.map(run => <details key={run.run_id}><summary>{run.strategy_version} · {run.evidence_kind} · {run.disposition}</summary>
          <p>Universe: {run.universe.as_of} · Cutoff: {run.knowledge_cutoff}</p>
          <p>Run: {run.run_id}</p>
          <table><thead><tr><th>Instrument</th><th>Rank</th><th>Score</th><th>Target weight</th></tr></thead>
            <tbody>{run.scores.map(s => <tr key={s.entity_id}><td>{s.entity_id}</td><td>{s.rank}</td><td>{s.score}</td><td>{run.targets.find(t => t.entity_id === s.entity_id)?.weight ?? '0'}</td></tr>)}</tbody></table>
          <p>Exclusions: {Object.entries(run.exclusions).map(([k, v]) => `${k}: ${v}`).join('; ') || 'none'}</p>
          <pre>{JSON.stringify(run.evidence.exact_versions, null, 2)}</pre>
        </details>)}
      </section>
      <section className="card"><h2>Comparison to control / previous version</h2>
        {lab.comparisons.length === 0 ? <p>No comparison with common frozen inputs.</p> : lab.comparisons.map(c => <p key={`${c.candidate_run}-${c.control_run}`}>{c.candidate_run} versus {c.control_run} · {c.common_snapshot}</p>)}
      </section>
      <section className="card"><h2>Execution and reconciliation</h2>
        {(lab.execution.executions ?? []).length === 0 && <p>No broker-paper lifecycle observed.</p>}
        {(lab.execution.executions ?? []).map(e => <details key={e.intent_id}><summary>{e.intent_id} · {e.state}</summary>
          <p>Broker order state: {e.broker_state}</p><p>Reconciliation: {e.reconciliation_clean ? 'clean' : e.findings.join(', ')}</p>
          <p>Exact fill economics: {e.exact_economics ? 'admissible' : 'unavailable'} · Evidence: {e.evidence_verdict}</p>
          <pre>{JSON.stringify(e.records, null, 2)}</pre>
        </details>)}
      </section>
    </>}
  </AppShell>
}
