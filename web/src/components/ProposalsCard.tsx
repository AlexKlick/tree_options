// LLM watchlist proposals (M6): the model suggests, the operator decides.
// Nothing here mutates the watchlist directly — Approve/Dismiss spool an
// idempotent op the discovery runner applies; the next poll shows it.

import { useState } from 'react'
import { decideProposal, requestProposals } from '../lib/api'
import type { ProposalRun, WatchProposal } from '../lib/types'
import { Pill } from './Pill'

function runLine(run: ProposalRun | null | undefined, now: number): string {
  if (!run) return 'never asked yet'
  const mins = Math.max(0, Math.round((now - Date.parse(run.at)) / 60_000))
  const when = mins < 90 ? `${mins}m ago` : `${Math.round(mins / 60)}h ago`
  if (run.status !== 'ok') return `last attempt ${when} failed — ${run.notes.slice(0, 2).join('; ')}`
  return `last asked ${when} via ${run.provider} · ${run.model} · ${run.added} new`
}

export function ProposalsCard({
  proposals,
  lastRun,
  onChanged,
}: {
  proposals: WatchProposal[]
  lastRun: ProposalRun | null | undefined
  onChanged: () => void
}) {
  const [asked, setAsked] = useState(false)
  const [decided, setDecided] = useState<Set<string>>(new Set())

  const ask = async () => {
    try {
      await requestProposals()
      setAsked(true)
      onChanged()
    } catch {
      /* 503 / portal 403: the run line stays honest */
    }
  }

  const decide = async (op: 'approve' | 'dismiss', id: string) => {
    try {
      await decideProposal(op, id)
      setDecided((prev) => new Set(prev).add(id))
      onChanged()
    } catch {
      /* the next poll reveals the truth */
    }
  }

  const visible = proposals.filter((p) => !decided.has(p.id))
  return (
    <section className="card proposals-card" aria-label="LLM watchlist proposals">
      <div className="card-head">
        <h3 style={{ margin: 0 }}>Proposals</h3>
        <button type="button" className="chip" onClick={ask}>
          {asked ? 'asked — runner thinking…' : 'Ask for ideas'}
        </button>
      </div>
      <div className="pill-row" style={{ margin: '8px 0' }}>
        <Pill variant="empty">model suggests · you decide</Pill>
        <span className="muted" style={{ fontSize: '0.8rem' }}>
          {runLine(lastRun, Date.now())}
        </span>
      </div>
      {visible.length === 0 ? (
        <p className="muted" style={{ margin: 0 }}>
          No pending proposals. Tickers are checked against a live CBOE quote before
          they reach you; dismissed ideas stay quiet for a week.
        </p>
      ) : (
        <ul className="proposal-list">
          {visible.map((p) => (
            <li key={p.id} className="proposal-row">
              <div className="proposal-main">
                <span className={`badge ${p.action === 'add' ? 'badge-open' : 'badge-exit_working'}`}>
                  {p.action}
                </span>{' '}
                <a href={`#/market/${p.symbol}`}>
                  <strong>{p.symbol}</strong>
                </a>
                {p.confidence != null && (
                  <span className="muted num"> · {Math.round(p.confidence * 100)}%</span>
                )}
                <div className="muted proposal-why">{p.rationale || 'no rationale given'}</div>
                {p.provenance?.model && (
                  <div className="muted proposal-prov">
                    {p.provenance.provider} · {p.provenance.model}
                  </div>
                )}
              </div>
              <div className="proposal-actions">
                <button
                  type="button"
                  className="chip"
                  onClick={() => decide('approve', p.id)}
                  aria-label={`Approve ${p.action} ${p.symbol}`}
                >
                  approve
                </button>
                <button
                  type="button"
                  className="chip chip-alt"
                  onClick={() => decide('dismiss', p.id)}
                  aria-label={`Dismiss ${p.action} ${p.symbol}`}
                >
                  dismiss
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
