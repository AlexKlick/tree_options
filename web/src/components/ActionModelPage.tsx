import { useState } from 'react'
import { getActionModelExample, getPlans } from '../lib/api'
import type { ActionNode } from '../lib/types'
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

export function ActionModelPage() {
  const poll = usePoll(getActionModelExample, 0)
  const accountPoll = usePoll(getPlans, 30_000)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const plan = poll.data?.plan
  const selected = plan?.nodes.find((node) => node.id === selectedId) ?? plan?.nodes[0]

  return (
    <AppShell title="Action model" poll={poll}>
      <section className="card" aria-label="Trading system observations">
        <div className="eyebrow">Existing TREX paper system · local observations</div>
        <h2>Trading boundary</h2>
        <p>Governed entry: disabled. No mandate, effect permit, or broker dispatch path is installed for this action model.</p>
        {accountPoll.data?.account ? (
          <p>Broker account snapshot: <code>{accountPoll.data.account.account_id}</code> · observed {accountPoll.data.account.ts} · age {accountPoll.data.account.age_seconds ?? 'unknown'} seconds. Snapshot equity does not set a strategy budget.</p>
        ) : (
          <p className="muted">Broker account snapshot unavailable in this view.</p>
        )}
        {accountPoll.data?.net_positions && <p>Existing TREX positions: {accountPoll.data.net_positions.length} underlying rows. Their legacy monitor remains the owner; this action model does not claim or control them.</p>}
        {accountPoll.error && <p className="muted">Account observation unavailable: {accountPoll.error}</p>}
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
