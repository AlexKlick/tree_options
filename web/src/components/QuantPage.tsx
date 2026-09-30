import { getQuantLab } from '../lib/api'
import { usePoll } from '../hooks/usePoll'
import { AppShell } from './AppShell'
import type { QuantTheoryCampaign } from '../lib/types'

function ratio(value: string | null, percent: boolean): string {
  if (value === null || !Number.isFinite(Number(value))) return 'unavailable'
  return `${(Number(value) * (percent ? 100 : 1)).toFixed(2)}${percent ? '%' : '×'}`
}

function TheoryCampaign({ campaign }: { campaign: QuantTheoryCampaign }) {
  const nodeAnchor = (id: string) => `theory-${campaign.campaign_id}-${id}`
  return <article>
    <h3>{campaign.hypothesis}</h3>
    <p>{campaign.data_class === 'synthetic_fixture' ? 'SYNTHETIC BACKTEST' : 'SIMULATED EXECUTION · USER-SUPPLIED UNQUALIFIED'}</p>
    <p>Exploratory retrospective · {campaign.disposition} · Execution disabled · Exact external economics unavailable</p>
    <p>Independent next-session open → close roundtrips</p>
    <p>Objective: mean net return minus endpoint loss and turnover penalties. Returns use independent equal starting capital; no compounded NAV.</p>
    <p>Candidates: {campaign.candidate_count} · Reflection calls: {campaign.reflection_calls}</p>
    <p>Winner: {campaign.winner.strategy_id} · Version: {campaign.winner.version_id}</p>
    <pre aria-label="Winner parameters">{JSON.stringify(campaign.winner.parameters, null, 2)}</pre>
    <table aria-label="Held-out comparison"><thead><tr>
      <th>Held-out lane</th><th>Periods scored</th><th>Mean net return</th><th>Endpoint loss</th><th>Turnover / starting capital</th>
    </tr></thead><tbody>{(['candidate', 'control'] as const).map(lane => {
      const metric = campaign.holdout[lane]
      return <tr key={lane}><th>{lane === 'candidate' ? 'Candidate' : 'Control'}</th>
        <td>{metric.scored_period_count}/{metric.period_count} · {metric.disposition}</td>
        <td>{ratio(metric.mean_net_return, true)}</td><td>{ratio(metric.max_drawdown, true)}</td><td>{ratio(metric.turnover, false)}</td>
      </tr>
    })}</tbody></table>
    <p>Endpoint loss measures the worst completed roundtrip loss, not intraday drawdown. Turnover counts gross buy and sell notional.</p>
    <ul aria-label="Campaign limitations">{campaign.limitations.map((text, index) => <li key={index}>{text}</li>)}</ul>
    <details><summary>Durable theory DAG · {campaign.campaign_id}</summary>
      <p>Persisted research nodes; no execution authority.</p>
      <table><thead><tr><th>Stage</th><th>Node</th><th>Payload digest</th><th>Parents</th></tr></thead>
        <tbody>{campaign.graph.map(node => <tr key={node.node_id} id={nodeAnchor(node.node_id)}>
          <th>{node.stage}</th><td>{node.node_id}</td><td>{node.payload_sha256}{node.payload_ref && <p>{node.payload_ref}</p>}</td>
          <td>{node.parents.length === 0 ? 'root' : node.parents.map(parent => <a key={parent} href={`#${nodeAnchor(parent)}`}>{parent}</a>)}</td>
        </tr>)}</tbody>
      </table>
    </details>
  </article>
}

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
      <section className="card"><h2>Theory campaigns</h2>
        {(lab.theory_campaigns ?? []).length === 0 && <p>No persisted theory campaign.</p>}
        {(lab.theory_campaigns ?? []).map(campaign => <TheoryCampaign key={campaign.campaign_id} campaign={campaign} />)}
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
        <details><summary>Campaign, target, risk and permit provenance</summary>
          <pre>{JSON.stringify({campaigns: lab.campaigns ?? [], risk: lab.execution.risk, edges: lab.execution.provenance ?? []}, null, 2)}</pre>
        </details>
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
