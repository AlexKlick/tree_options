import { useState } from 'react'
import { getActionModelExample, getHistoricalReplays, getIntradayGraphs, getLabScoreboard, getPlans, getPortfolioScenarios, getSupervisedDesk } from '../lib/api'
import type { ActionNode, LabScoreboard, SupervisedDeskStatus } from '../lib/types'
import { usePoll } from '../hooks/usePoll'
import { AppShell } from './AppShell'

function NodeInspector({ node }: { node: ActionNode }) {
  return (
    <section className="card action-inspector" aria-label="Selected action details">
      <div className="eyebrow">{node.id} · {node.effect_class}</div>
      <h2>{node.label}</h2>
      <p className="muted">{node.operation}@{node.operation_version} · {node.owner_role} · {node.target_environment}</p>
      <h3>Required inputs</h3>
      <ul>
        {Object.entries(node.inputs).map(([port, input]) => (
          <li key={port}><strong>{port}</strong>: {input.expected_type} from{' '}
            <code>{input.artifact_id ?? `${input.producer_node_id}.${input.output_name}`}</code>
          </li>
        ))}
      </ul>
      <h3>Guards and evidence</h3>
      <p>Guards: {node.required_guards.join(', ') || 'none declared'}</p>
      <p>Receipts required: {node.required_receipts.join(', ')}</p>
      <p>Postcondition: <code>{node.postcondition}</code></p>
      <p className="muted">No attempt, grant, permit, broker effect, or result is recorded for this synthetic node.</p>
    </section>
  )
}

function DeskStatusLines({ status }: { status: SupervisedDeskStatus }) {
  const mandate = status.supervised.mandate
  const authority = mandate.state === 'active' || mandate.state === 'expired'
    ? `Mandate ${mandate.state} · ${mandate.orders_used ?? 0}/${mandate.max_orders ?? '?'} orders used · ${mandate.days_left ?? 0} day${mandate.days_left === 1 ? '' : 's'} left · ${mandate.long_running ? 'long-running grant' : 'single-session grant'}`
    : mandate.state === 'revoked'
      ? 'Mandate revoked — the tombstone is permanent; re-arming is an operator action.'
      : 'No mandate installed — the supervised chain currently holds no trading authority.'
  const killFiles = status.kill_files.length > 0
    ? `Kill files: ${status.kill_files.join(', ')} — the desk refuses new entries while these stand.`
    : 'Kill files: none.'
  const entries = Object.entries(status.book ?? {})
  const book = status.book === null
    ? 'Book: no structures recorded yet.'
    : entries.length === 0
      ? 'Book: flat.'
      : `Book: ${entries.length} structure${entries.length === 1 ? '' : 's'} — ${entries.map(([id, row]) => `${id} ${row.status ?? 'unknown'} (qty ${row.open_qty})`).join(', ')}.`
  return (
    <>
      <p>{authority}</p>
      <p>{killFiles}</p>
      <p>Entry requests pending: {status.inbox.length}</p>
      <p>{book}</p>
      {status.last_results.length === 0
        ? <p className="muted">No entry request has been processed yet.</p>
        : <ul>{status.last_results.map((result) => (
          <li key={result.request ?? result.intent_id}>
            <code>{result.request ?? result.intent_id}</code>: {result.status}
            {result.reason ? ` — ${result.reason}` : ''}
            {result.blockers && result.blockers.length > 0 ? ` — ${result.blockers.join(', ')}` : ''}
          </li>
        ))}</ul>}
    </>
  )
}

function LabScoreboardBlock({ board }: { board: LabScoreboard }) {
  const policies = Object.entries(board.policies)
  return (
    <>
      <h3>Lab scoreboard</h3>
      {policies.length === 0
        ? <p className="muted">No lab run has completed yet.</p>
        : <ul>{policies.map(([name, stats]) => (
          <li key={name}>{name}: {stats.runs} runs · {stats.entered} entered · {stats.modeled_wins}W/{stats.modeled_losses}L · closed-pnl proxy ${stats.closed_pnl_sum}</li>
        ))}</ul>}
      {board.advisory
        ? <p className="muted">Advisory: {board.advisory.policy} leads — {board.advisory.basis}. Advisory only, never promoted.</p>
        : <p className="muted">No policy has enough runs for an advisory yet.</p>}
    </>
  )
}

export function ActionModelPage() {
  const poll = usePoll(getActionModelExample, 0)
  const accountPoll = usePoll(getPlans, 30_000)
  const desk = usePoll(getSupervisedDesk, 30_000)
  const lab = usePoll(getLabScoreboard, 60_000)
  const replays = usePoll(getHistoricalReplays, 60_000)
  const portfolios = usePoll(getPortfolioScenarios, 60_000)
  const intraday = usePoll(getIntradayGraphs, 60_000)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const plan = poll.data?.plan
  const selected = plan?.nodes.find((node) => node.id === selectedId) ?? plan?.nodes[0]

  return (
    <AppShell title="Action model" poll={poll}>
      <section className="card" aria-label="Supervised paper desk">
        <div className="eyebrow">Existing TREX paper system · local observations</div>
        <h2>Supervised desk (paper)</h2>
        <p>Governed entry runs on the supervised desk — a separate armed path with its own operator mandate, effect permits, and IBKR paper broker session. This cockpit only reads the desk's on-disk state; it never contacts the broker. The synthetic action-model plan on this page remains unhooked: no mandate, permit, or dispatch path is installed for it.</p>
        {desk.error && <p role="alert">Supervised desk status unavailable: {desk.error}</p>}
        {desk.data && <DeskStatusLines status={desk.data} />}
        {lab.error && <p role="alert">Lab scoreboard unavailable: {lab.error}</p>}
        {lab.data && <LabScoreboardBlock board={lab.data} />}
        {accountPoll.data?.account ? (
          <p>Broker account snapshot: <code>{accountPoll.data.account.account_id}</code> · observed {accountPoll.data.account.ts} · age {accountPoll.data.account.age_seconds ?? 'unknown'} seconds. Snapshot equity does not set a strategy budget.</p>
        ) : (
          <p className="muted">Broker account snapshot unavailable in this view.</p>
        )}
        {accountPoll.data?.net_positions && <p>Existing TREX positions: {accountPoll.data.net_positions.length} underlying rows. Their legacy monitor remains the owner; this action model does not claim or control them. {accountPoll.data.net_positions.length > 0 && 'The supervised governed canary remains blocked until the legacy book is flat.'}</p>}
        {accountPoll.error && <p className="muted">Account observation unavailable: {accountPoll.error}</p>}
      </section>
      <section className="card" aria-label="Historical options replay">
        <div className="eyebrow">Historical options · modeled evidence</div>
        <h2>Replay history</h2>
        <p className="muted">Cached daily option VWAPs are modeled prices, not broker fills. Runs do not place orders or authorize a strategy.</p>
        {replays.error && <p role="alert">Replay reports unavailable: {replays.error}</p>}
        {replays.data?.reports.length === 0 && <p>No historical replay has completed in this cockpit store.</p>}
        {replays.data?.reports.map((run) => (
          <div key={run.id} className="card">
            <h3>{run.spec.start} through {run.spec.end}</h3>
            <p>{run.spec.names.length} names · {run.spec.entry_dte.join('–')} entry DTE · assumed haircut {(run.spec.haircut * 100).toFixed(1)}% per leg · ${run.spec.max_loss} trade cap</p>
            <p>{run.counts.evaluable_within_trade_cap ?? 0} evaluable of {run.counts.attempted ?? 0} attempted · {run.counts.missing_decision_spot_or_options ?? 0} missing decision data before attempts · {run.counts.missing_exit_or_entry_bar ?? 0} missing bars · {run.counts.over_trade_loss_cap ?? 0} above cap</p>
            <ul>{Object.entries(run.by_variant).map(([name, row]) => (
              <li key={name}>{name}: {row.trades} modeled trades · {row.trades < 20 ? `${row.wins} modeled wins; too few trades for a rate` : row.win_rate === null ? 'win rate unavailable' : `${(row.win_rate * 100).toFixed(1)}% modeled wins`} · mean {row.mean_pnl === null ? 'unavailable' : `$${row.mean_pnl.toFixed(2)}`} · worst {row.worst_pnl === null ? 'unavailable' : `$${row.worst_pnl.toFixed(2)}`}{run.eligibility_by_variant?.[name] && <> · eligibility: {Object.entries(run.eligibility_by_variant[name]).map(([status, count]) => `${status} ${count}`).join(', ')}</>}</li>
            ))}</ul>
            <p className="muted">{run.id} · {run.provenance.sources.length} cache sets · {run.limitations.join('; ')}</p>
          </div>
        ))}
      </section>
      <section className="card" aria-label="Intraday action graphs">
        <div className="eyebrow">Historical minute bars · research only</div>
        <h2>Intraday action graphs</h2>
        <p className="muted">Each window has scheduled snapshots, available spread candidates, and a separate chosen-action path. Option trade prices are valuation proxies, not executable quotes or broker fills. Check each run's dates before pooling windows.</p>
        <p><a href="#/trade-floor">Watch the three model traders on the virtual floor</a></p>
        {intraday.error && <p role="alert">Intraday graph summaries unavailable: {intraday.error}</p>}
        {intraday.data?.reports.length === 0 && <p>No intraday action graph has completed in this cockpit store.</p>}
        {intraday.data?.reports.map((run) => (
          <div key={run.id} className="card">
            <h3>{run.policy} · {run.id}</h3>
            <p>{run.captured_contracts}/{run.requested_contracts} captured option series · {run.traded_minute_bars} traded-minute bars</p>
            <ul>{run.windows.map((window) => (
              <li key={`${window.start}-${window.end}`}>{window.start} through {window.end}: {window.scheduled_snapshots} snapshots across {window.sessions} sessions · {window.potential_trades} potential spread nodes · {window.entered} modeled entries · {window.modeled_wins} wins, {window.modeled_losses} losses from later trade-bar marks · ${window.peak_open_loss_reserved} peak reserved loss · ${window.closed_capital_proxy} closed capital proxy{window.open_at_end > 0 ? ` · ${window.open_at_end} still open at cutoff` : ''}</li>
            ))}</ul>
            <p className="muted">Source SHA-256 {run.source_sha256.slice(0, 12)} · {run.limitations.join('; ')} · execution disabled</p>
          </div>
        ))}
      </section>
      <section className="card" aria-label="Portfolio risk scenarios">
        <div className="eyebrow">$5,000 target · modeled overlap</div>
        <h2>Portfolio risk scenarios</h2>
        <p className="muted">Each fixed strategy variant is projected separately. Daily option bars cannot prove an intraday or daily loss stop, broker fills, or a profitable strategy.</p>
        {portfolios.error && <p role="alert">Portfolio scenarios unavailable: {portfolios.error}</p>}
        {portfolios.data?.reports.length === 0 && <p>No portfolio scenario has completed in this cockpit store.</p>}
        {portfolios.data?.reports.map((run) => (
          <div key={run.id} className="card">
            <h3>{run.id}</h3>
            <p>${run.spec.intended_capital} target capital · ${run.spec.max_trade_loss} per modeled trade · ${run.spec.max_open_loss} combined open loss cap</p>
            <ul>{Object.entries(run.variants).map(([name, row]) => (
              <li key={name}>{name}: {row.admitted}/{row.considered} modeled entries admitted · {row.skipped.open_cap} blocked by open-risk cap · ${row.peak_open_loss_reserved} peak reserved loss · ${row.closed_pnl} closed modeled P&amp;L</li>
            ))}</ul>
            <p className="muted">Replay SHA-256 {run.provenance.replay_sha256.slice(0, 12)} · {run.provenance.code_dirty ? 'code tree was dirty' : 'clean code tree'} · {run.limitations.join('; ')}</p>
          </div>
        ))}
      </section>
      <section className="card" aria-label="Trading system graph">
        <div className="eyebrow">System graph · current source boundary</div>
        <h2>From historical evidence to paper trading</h2>
        <ol>
          <li>Option bars and locked signal data → exploratory replay → source-bound report → read-only cockpit.</li>
          <li>Forecast and pricing → governed candidate: evidence binding and capital mandate still needed.</li>
          <li>Governed candidate → IBKR paper order: permit, reconciliation, and supervised receipt still needed.</li>
        </ol>
        <p className="muted">The existing IBKR login and legacy position monitor are separate from this governed path. No replay or model review enables trading.</p>
      </section>
      <div className="card">
        <div className="eyebrow">Proposal only · synthetic design example</div>
        <h2>{plan?.goal ?? 'Governed action plan'}</h2>
        <p className="muted">This is a checked design fixture. It has no market observations, active mandate, execution attempts, or broker receipts. Selecting a step only opens its declared contract.</p>
        {plan && <p><code>{plan.plan_id}</code> · revision {plan.revision} · source {plan.source_repository_head.slice(0, 12)}</p>}
        {poll.data && <p>Structure: {poll.data.receipt.valid_structure ? 'valid' : 'invalid'} · {poll.data.receipt.node_count} proposed steps · {poll.data.receipt.dependency_count} dependencies · execution authorized: no</p>}
      </div>
      {poll.error && <div className="card" role="alert">Action model unavailable: {poll.error}</div>}
      {plan && <div className="action-layout">
        <section aria-label="Proposed actions">
          <h2 className="section-title">Proposed sequence</h2>
          <div className="action-node-list">
            {plan.nodes.map((node) => (
              <button type="button" className={`card action-node ${selected?.id === node.id ? 'action-node-selected' : ''}`}
                key={node.id} onClick={() => setSelectedId(node.id)} aria-pressed={selected?.id === node.id}>
                <span className="eyebrow">{node.id} · {node.effect_class}</span>
                <strong>{node.label}</strong>
                <span className="muted">{node.dependencies.length ? `After ${node.dependencies.map((d) => `${d.node_id} (${d.on_outcomes.join('/')})`).join(', ')}` : 'Start node'}</span>
              </button>
            ))}
          </div>
        </section>
        {selected && <NodeInspector node={selected} />}
      </div>}
    </AppShell>
  )
}
