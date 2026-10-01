import { useRef, useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import * as api from '../lib/workspaceApi'
import type { PaperAccount, PaperAllocation, PaperDeployment, ResearchDataset, ResearchJob } from '../lib/workspaceTypes'
import { AppShell } from './AppShell'
import { TableScroll } from './TableScroll'

const usd = (value: string) => Number(value).toLocaleString('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 0})
const percent = (value?: string | null) => value != null && Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(2)}%` : 'Unavailable'
type RecordedResult = {disposition?: string; winner?: {strategy_id?: string; version_id?: unknown}; holdout?: {candidate?: {mean_net_return?: string | null; fees?: string; period_count?: number}; control?: {mean_net_return?: string | null; fees?: string; period_count?: number}}}
const message = (error: unknown) => error instanceof Error ? error.message : 'Request failed'

async function snapshot(previous?: {jobs: ResearchJob[]; accounts: PaperAccount[]; plans: PaperAllocation[]; deployments: PaperDeployment[]; datasets: ResearchDataset[]; paperControls: boolean} | null) {
  const responses = await Promise.allSettled([
    api.getResearchDatasets(), api.getResearchJobs(), api.getPaperAccounts(),
    api.getPaperDeployments(), api.getPaperAllocations(),
  ] as const)
  return {
    datasets: responses[0].status === 'fulfilled' ? responses[0].value.datasets : previous?.datasets ?? [],
    jobs: responses[1].status === 'fulfilled' ? responses[1].value.jobs : previous?.jobs ?? [],
    accounts: responses[2].status === 'fulfilled' ? responses[2].value.accounts : previous?.accounts ?? [],
    deployments: responses[3].status === 'fulfilled' ? responses[3].value.deployments : previous?.deployments ?? [],
    plans: responses[4].status === 'fulfilled' ? responses[4].value.plans : previous?.plans ?? [],
    researchControls: responses[1].status === 'fulfilled' && responses[1].value.controls_enabled === true,
    paperControls: responses[2].status === 'fulfilled' ? responses[2].value.controls_enabled === true : previous?.paperControls ?? false,
    datasetReadOk: responses[0].status === 'fulfilled',
    accountsReadOk: responses[2].status === 'fulfilled',
    deploymentsReadOk: responses[3].status === 'fulfilled',
    allocationsReadOk: responses[4].status === 'fulfilled',
    errors: responses.flatMap((r, i) => r.status === 'rejected' ? [`${['datasets', 'research jobs', 'paper accounts', 'paper deployments', 'allocations'][i]}: ${message(r.reason)}`] : []),
  }
}

export function ResearchWorkspacePage() {
  const cached = useRef<Awaited<ReturnType<typeof snapshot>> | null>(null)
  const poll = usePoll(async () => {
    const next = await snapshot(cached.current)
    cached.current = next
    return next
  }, 3000)
  const data = poll.data
  const [hypothesis, setHypothesis] = useState('')
  const [dataset, setDataset] = useState('')
  const [capital, setCapital] = useState('50000')
  const [strategy, setStrategy] = useState('equal_weight_us_equities')
  const [candidates, setCandidates] = useState(8)
  const [generations, setGenerations] = useState(1)
  const [reflect, setReflect] = useState(false)
  const [sleeve, setSleeve] = useState('')
  const [account, setAccount] = useState('')
  const evidenceSelection = useRef(0)
  const [researchJob, setResearchJob] = useState('')
  const [strategyVersion, setStrategyVersion] = useState('operational-canary/1')
  const [candidateVersion, setCandidateVersion] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [detail, setDetail] = useState<Awaited<ReturnType<typeof api.getResearchJob>> | null>(null)
  const [allocationKey] = useState(() => crypto.randomUUID())
  const [proposalKey, setProposalKey] = useState(() => crypto.randomUUID())
  const recordedResult = detail?.result as RecordedResult | null
  const sleeves = data?.plans.flatMap(p => p.sleeves) ?? []
  const selectedSleeve = sleeves.find(s => s.sleeve_id === sleeve)
  const selectedDataset = data?.datasets.find(d => d.dataset_id === dataset)
  const researchEnabled = data?.researchControls && data.datasetReadOk && !busy && !poll.error
  const paperEnabled = data?.paperControls && data.accountsReadOk && data.deploymentsReadOk && data.allocationsReadOk && !busy && !poll.error
  const haltEnabled = data?.paperControls && !busy

  async function act(work: () => Promise<unknown>, success: string) {
    setBusy(true); setError(''); setNotice('')
    try { await work(); setNotice(success); poll.refresh() }
    catch (err) { setError(message(err)) }
    finally { setBusy(false) }
  }

  function chooseSleeve(id: string) {
    setSleeve(id)
    const found = sleeves.find(s => s.sleeve_id === id)
    if (found) { setCapital(found.capital_usd); setAccount(found.account_alias ?? '') }
  }

  async function chooseResearch(id: string) {
    const selection = ++evidenceSelection.current
    setResearchJob(id); setDetail(null); setCandidateVersion(''); setStrategyVersion('operational-canary/1')
    if (!id) return
    await act(async () => {
      const evidence = await api.getResearchJob(id)
      if (selection !== evidenceSelection.current) return
      const result = evidence.result as {winner?: {version_id?: unknown}} | null
      const version = result?.winner?.version_id
      if (typeof version === 'string' && /^[a-z0-9_]+\/v[1-9][0-9]*\/[a-f0-9]{64}$/.test(version)) {
        setCandidateVersion(version); setStrategyVersion(version)
      }
      setDetail(evidence)
    }, 'Recorded strategy evidence selected. Qualification remains required.')
  }

  return <AppShell title="Research and paper" poll={poll} showRuntimeBanners={false}
    contextLabel="trex · supervised workspace"
    footerSource="Durable research jobs and paper proposals · broker effects owned by the supervised runtime">
    <div className="research-workspace">
      <p>Assign research → test a theory → review evidence → propose paper deployment.</p>
      <p>Equities first; the existing options desk remains a separate execution owner. <a href="#/quant">Open evidence and provenance</a>.</p>
      {!data && <p role="status">Loading workspace…</p>}
      {data && !data.researchControls && !data.paperControls && <p>Operator controls are disabled. This workspace is available for inspection.</p>}
      <p className="muted">Research and proposal controls require an enabled local operator session. Trading approval happens separately against an exact account, strategy version and expiring mandate.</p>
      {data?.errors.map(e => <p className="error" key={e}>{e}</p>)}
      {error && <p role="alert" className="error">{error}</p>}
      {notice && <p role="status">{notice}</p>}

      <section className="card">
        <h2>Assign research</h2>
        <form onSubmit={e => {
          e.preventDefault()
          void act(() => api.assignResearch({hypothesis, dataset_id: dataset, capital,
            max_candidates: candidates, generations, strategy_id: strategy, top_n: null,
            reflect_glm53: reflect, ...(sleeve ? {sleeve_id: sleeve} : {})}), 'Research queued. No broker orders authorized.')
        }}>
          <label>Research hypothesis<textarea required minLength={8} maxLength={2000} value={hypothesis} onChange={e => setHypothesis(e.target.value)} placeholder="Does 12-minus-1 momentum beat equal weight after costs on a protected holdout?" /></label>
          <div className="workspace-fields">
            <label>Frozen dataset<select aria-describedby="workspace-selected-dataset" required value={dataset} onChange={e => setDataset(e.target.value)}>
              <option value="">Choose a dataset</option>
              {data?.datasets.map(d => <option key={d.dataset_id} value={d.dataset_id}>{d.label} · {d.data_class === 'synthetic_fixture' ? 'SYNTHETIC BACKTEST' : 'UNQUALIFIED DATA'}</option>)}
            </select></label>
            <label>Experiment sleeve<select value={sleeve} onChange={e => chooseSleeve(e.target.value)}>
              <option value="">Standalone research</option>{sleeves.map(s => <option key={s.sleeve_id} value={s.sleeve_id}>{s.label} · {usd(s.capital_usd)}</option>)}
            </select></label>
            <label>Intended capital<select value={capital} disabled={Boolean(selectedSleeve)} onChange={e => setCapital(e.target.value)}><option value="50000">$50,000</option><option value="5000">$5,000</option></select></label>
            <label>Strategy family<select value={strategy} onChange={e => setStrategy(e.target.value)}>
              <option value="equal_weight_us_equities">Equal weight</option><option value="momentum_12_1">12-minus-1 momentum</option><option value="hqm_1_3_6_12">Multi-horizon momentum</option>
            </select></label>
            <label>Candidate budget<input type="number" min={2} max={32} required value={candidates} onChange={e => setCandidates(Number(e.target.value))} /></label>
            <label>Reflection generations<input type="number" min={0} max={4} required value={generations} onChange={e => setGenerations(Number(e.target.value))} /></label>
          </div>
          <p id="workspace-selected-dataset">{selectedDataset
            ? <><strong>{selectedDataset.data_class === 'synthetic_fixture' ? 'SYNTHETIC BACKTEST' : 'UNQUALIFIED DATA'}</strong> · {selectedDataset.label}</>
            : 'Select a frozen dataset to see its evidence class.'}</p>
          <label className="workspace-check"><input type="checkbox" checked={reflect} onChange={e => setReflect(e.target.checked)} />Use bounded GLM-5.3 research reflection (provider calls)</label>
          {selectedDataset?.splits && <p>Frozen decision windows: {Object.entries(selectedDataset.splits).map(([name, split]) => `${name}: ${split.decision_start} to ${split.decision_end} (${split.period_count} periods)`).join(' · ')} · {selectedDataset.universe_count} universe members.</p>}
          <p>Costs and portfolio mechanics are fixed by the registered protocol. Current campaigns model independent next-session roundtrips; they do not establish a persistent portfolio return.</p>
          {data?.datasets.length === 0 && <p>No frozen research datasets registered.</p>}
          <button type="submit" disabled={!researchEnabled || !dataset || hypothesis.trim().length < 8}>Assign research</button>
        </form>
      </section>

      <section className="card">
        <h2>Research jobs</h2>
        {data?.jobs.length === 0 && <p>No research assignments yet.</p>}
        {data?.jobs.map(job => <article className="workspace-item" key={job.run_id}>
          <h3>{job.hypothesis}</h3><p>{job.status} · {usd(job.capital)} · {job.data_class === 'synthetic_fixture' ? 'SYNTHETIC BACKTEST' : 'UNQUALIFIED BACKTEST'}</p>
          <p>Strategy: {job.strategy_id} · candidates ≤ {job.max_candidates} · sleeve: {job.sleeve_id ?? 'standalone'}</p>
          {job.error && <p className="error">{job.error}</p>}
          <div className="workspace-actions">
            <button type="button" disabled={busy} onClick={() => void act(async () => {setDetail(await api.getResearchJob(job.run_id))}, 'Evidence loaded.')}>View evidence {job.run_id}</button>
            {['queued', 'running', 'stopping'].includes(job.status) && <button type="button" disabled={!researchEnabled} onClick={() => void act(() => api.controlResearchJob(job.run_id, 'stop'), 'Stop recorded durably. An in-flight observation may finish.')}>Stop research {job.run_id}</button>}
            {job.status === 'stopped' && <button type="button" disabled={!researchEnabled} onClick={() => void act(() => api.controlResearchJob(job.run_id, 'resume'), 'Resume queued with the same frozen inputs.')}>Resume research {job.run_id}</button>}
          </div>
        </article>)}
        {detail && <details open><summary>Recorded evidence: {detail.job.run_id}</summary>
          <p>Evidence remains research; no trading authority is granted.</p>
          <p>{detail.job.data_class === 'synthetic_fixture' ? 'SYNTHETIC BACKTEST' : 'UNQUALIFIED BACKTEST'} · <span>{recordedResult?.disposition ?? detail.job.status}</span></p>
          {recordedResult?.winner && <p>Selected research candidate: {recordedResult.winner.strategy_id}</p>}
          {recordedResult?.holdout && <TableScroll label="Research comparison columns"><table className="retain-row-identity" aria-label="Recorded control comparison"><thead><tr><th>Held-out comparison</th><th>Modeled mean net per roundtrip</th><th>Modeled fees (USD)</th><th>Periods</th></tr></thead><tbody>
            <tr><td>Candidate</td><td>{percent(recordedResult.holdout.candidate?.mean_net_return)}</td><td>{recordedResult.holdout.candidate?.fees ?? 'Unavailable'}</td><td>{recordedResult.holdout.candidate?.period_count ?? 'Unavailable'}</td></tr>
            <tr><td>Control</td><td>{percent(recordedResult.holdout.control?.mean_net_return)}</td><td>{recordedResult.holdout.control?.fees ?? 'Unavailable'}</td><td>{recordedResult.holdout.control?.period_count ?? 'Unavailable'}</td></tr>
          </tbody></table></TableScroll>}
          <p>These independent modeled roundtrips do not establish a persistent portfolio return or exact broker P&amp;L.</p>
          <details><summary>Full recorded result</summary><pre>{JSON.stringify(detail.result ?? {status: detail.job.status, result: 'not yet published'}, null, 2)}</pre></details>
          <details><summary>Provenance</summary><pre>{JSON.stringify(detail.provenance, null, 2)}</pre></details>
        </details>}
      </section>

      <section className="card">
        <h2>Paper capital allocation</h2>
        <p>19 experiments × $50,000 + 10 experiments × $5,000 = $1,000,000.</p>
        <p>These are virtual capital sleeves, not 29 newly opened brokerage accounts. Shared broker positions require fill attribution before separate exact P&amp;L is available.</p>
        <label>Paper account<select value={account} onChange={e => setAccount(e.target.value)}><option value="">Not connected yet</option>{data?.accounts.map(a => <option key={a.account_alias} value={a.account_alias}>{a.account_alias} · {a.qualification_status}</option>)}</select></label>
        <button type="button" disabled={!paperEnabled || Boolean(data?.plans.length)} onClick={() => void act(() => api.createPaperAllocation(account || null, allocationKey), '29 experiment sleeves created. No orders authorized.')}>Create allocation plan</button>
        {data?.plans.map(plan => <div key={plan.plan_id}>
          <p>Total assigned: {usd(plan.total_capital_usd)} · {plan.sleeves.length} sleeves</p>
          <TableScroll label="Paper allocation columns"><table className="retain-row-identity"><thead><tr><th>Experiment</th><th>Capital</th><th>Reserved</th><th>Account</th><th>Modeled research</th><th>Exact broker P&amp;L</th><th>Research</th></tr></thead><tbody>
            {plan.sleeves.map(s => {
              const related = data?.jobs.filter(j => j.sleeve_id === s.sleeve_id) ?? []
              const completed = related.filter(j => j.result_summary)
              const latest = completed[completed.length - 1]
              return <tr key={s.sleeve_id}><td>{s.label}</td><td>{usd(s.capital_usd)}</td><td>{usd(s.reserved_usd)}</td><td>{s.account_alias ?? 'Unbound'}<br /><button type="button" disabled={!paperEnabled || !account || Number(s.reserved_usd) !== 0 || s.account_alias === account} onClick={() => void act(() => api.bindPaperSleeve(plan.plan_id, s.sleeve_id, account), 'Sleeve account binding recorded. No orders authorized.')}>Bind {s.label} to selected account</button></td><td>{latest?.result_summary ? <>{latest.result_summary.data_class === 'synthetic_fixture' ? 'SYNTHETIC' : 'UNQUALIFIED'} · {latest.result_summary.disposition} · mean net {latest.result_summary.mean_net_return === null ? 'unavailable' : `${(Number(latest.result_summary.mean_net_return) * 100).toFixed(2)}%`}</> : `${related.length} assignments · no completed result`}</td><td>Unavailable</td><td><button type="button" onClick={() => chooseSleeve(s.sleeve_id)}>Use {s.label}</button></td></tr>
            })}
          </tbody></table></TableScroll>
        </div>)}
      </section>

      <section className="card">
        <h2>Connect and qualify paper accounts</h2>
        <p>Qualify your existing IBKR paper account first. Alpaca Paper through SnapTrade is also supported.</p>
        <ol><li>Use the existing supervised IBKR paper desk owner and its explicit state root. Read-only qualification must confirm fresh account truth; equity execution needs a separately qualified adapter.</li><li>For Alpaca Paper, connect through SnapTrade and populate TREX's private server-side binding locally. Keep credentials out of chat and browser forms.</li><li>Verify account identity, paper environment, balances, positions, orders and exclusive ownership.</li><li>Review an exact bounded canary mandate only after the chosen adapter and current quotes are qualified.</li></ol>
        <p>The setup runbook includes the exact commands. <a href="https://docs.snaptrade.com/docs/getting-started" target="_blank" rel="noreferrer">SnapTrade setup documentation</a>.</p>
        {data?.accounts.length === 0 && <p>No configured paper accounts. Broker-paper trading remains blocked.</p>}
        {data?.accounts.map(a => <article className="workspace-item" key={a.account_alias}><h3>{a.account_alias}</h3><p>{a.provider ?? 'snaptrade'} · {a.qualification_status}</p><p>Current runtime ownership must be rechecked before execution.</p>{a.provider === 'ibkr' && <p>IBKR equity execution: not qualified. Existing supervised options execution remains separate.</p>}<p>Last qualification: {a.assessed_at ?? 'not observed'} · expires: {a.expires_at ?? 'not qualified'}</p>{a.blockers.map(b => <p key={b}>{b}</p>)}</article>)}
      </section>

      <section className="card">
        <h2>Propose broker paper deployment</h2>
        <p>SnapTrade execution machinery: one integer-share operational canary, at most $100 gross and a short TTL. Campaign deployment remains gated by data, strategy and portfolio qualification.</p>
        <form onSubmit={e => {
          e.preventDefault()
          void act(async () => {
            await api.proposePaperDeployment({account_alias: account, strategy_version: strategyVersion,
              research_job_id: researchJob || null, ...(sleeve ? {sleeve_id: sleeve} : {}),
              intended_capital_usd: capital, max_gross_notional_usd: '100', max_orders: 1,
              ttl_seconds: 300, idempotency_key: proposalKey})
            setProposalKey(crypto.randomUUID())
          }, 'Paper proposal recorded for review. No order submitted.')
        }}>
          <div className="workspace-fields">
            <label>Research evidence<select value={researchJob} onChange={e => void chooseResearch(e.target.value)}><option value="">Operational canary (no strategy promotion)</option>{data?.jobs.filter(j => j.status === 'completed').map(j => <option key={j.run_id} value={j.run_id}>{j.hypothesis} · {j.run_id}</option>)}</select></label>
            <label>Strategy version<select required value={strategyVersion} onChange={e => setStrategyVersion(e.target.value)}><option value="operational-canary/1">operational-canary/1</option>{candidateVersion && <option value={candidateVersion}>{candidateVersion} · research candidate, qualification required</option>}</select></label>
          </div>
          <p>Account: {account || 'not connected'} · sleeve: {selectedSleeve?.label ?? 'not selected'} · intended capital: {usd(capital)}.</p>
          <button type="submit" disabled={!paperEnabled || !account || !sleeve}>Create paper proposal</button>
        </form>
        {(data?.deployments.length ?? 0) > 0 && <h3>Recorded paper proposals and deployments</h3>}
        {data?.deployments.map(d => <article className="workspace-item" key={d.deployment_id}>
          <h3>{d.strategy_version} · {d.account_alias}</h3><p>{d.status} · {d.execution_status}</p><p>Sleeve: {d.sleeve_id ?? 'unassigned'} · cap {usd(d.max_gross_notional_usd)} · {d.max_orders} order · TTL {d.ttl_seconds}s</p>
          {d.blockers.map(b => <p key={b}>{b}</p>)}
          {d.execution_status.endsWith('_RECONCILIATION_REQUIRED') && <p>Reserved capital stays held until the existing owner reconciles the broker effect, including after HALT.</p>}
          <p>Exact external fill economics remain unavailable until the fill source is validated. Live money is disabled.</p>
          <button type="button" disabled={!haltEnabled || d.status === 'HALTED'} onClick={() => void act(() => api.haltPaperDeployment(d.deployment_id), 'Halt recorded. Existing broker orders still require reconciliation.')}>Halt paper deployment {d.deployment_id}</button>
        </article>)}
      </section>
    </div>
  </AppShell>
}
