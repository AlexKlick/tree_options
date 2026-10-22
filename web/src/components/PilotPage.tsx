import { useState } from 'react'
import { usePoll } from '../hooks/usePoll'
import type { BudgetUsage, PilotRegistration } from '../lib/contracts'
import {
  getPilot,
  postSeal,
  sealCommandBody,
  type PilotProgramRow,
  type PilotView,
  type SealAnswer,
  type SealCommandBody,
} from '../lib/pilotApi'
import { AppShell } from './AppShell'
import './PilotPage.css'

// #/pilot and #/pilot/:programId (C3.3; destination Experiments). Read only,
// except one explicit owner act: sealing a draft registration, which queues a
// seal_registration command (the desk applies it). Nothing here starts a job,
// sends an order or authorizes live trading.

const ROLE_LABELS: Record<string, string> = {
  observation: 'Observation (existing, cited)',
  baseline: 'Baseline (control)',
  challenger: 'Challenger (preregistered)',
}

const BLOCKER_WORDS: Record<string, string> = {
  registration_not_sealed: 'Draft: cannot run until the owner seals it.',
  registration_sealer_not_owner: 'Sealed by someone who is not a current owner: cannot run.',
  registration_missing: 'No registration file: cannot run.',
  registration_unreadable: 'The registration file does not read: inspect it before anything runs.',
  budget_usage_unknown: "Today's usage is unknown: new jobs are refused until it is known.",
  budget_usage_mismatch: "Today's usage file belongs to another program: new jobs are refused.",
  budget_requests_exhausted: "Today's request budget is used up.",
  budget_compute_exhausted: "Today's compute budget is used up.",
  budget_storage_exhausted: "Today's storage budget is used up.",
}

const ROSTER_WORDS: Record<string, string> = {
  pilot_role_count: 'The pilot needs exactly one program in this role',
  pilot_control_unknown: 'A control names a program that is not in the pilot',
  pilot_program_repeated: 'A program id appears twice',
}

const link = (id: string) => `#/pilot/${encodeURIComponent(id)}`

// a zero budget is a decision (no pilot job runs under this id), not a used-up day
const ZERO_BUDGET_WORDS: Record<string, [keyof PilotRegistration['budgets'], string]> = {
  budget_requests_exhausted: ['requests_per_day', 'No pilot request budget: no pilot job runs under this program.'],
  budget_compute_exhausted: ['compute_minutes_per_day', 'No pilot compute budget.'],
  budget_storage_exhausted: ['storage_mb', 'No pilot storage budget.'],
}

function blockerText(code: string, registration?: PilotRegistration | null): string {
  const zero = ZERO_BUDGET_WORDS[code]
  if (zero && registration && registration.budgets[zero[0]] === 0) return zero[1]
  return BLOCKER_WORDS[code] ?? code
}

function rosterText(code: string): string {
  const [head, detail] = code.split(':', 2)
  const words = ROSTER_WORDS[head]
  return words ? `${words}${detail ? `: ${detail}` : ''}.` : code
}

function bytes(n: number): string {
  if (n < 1024) return `${n} bytes`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`
}

function UsageLines({ registration, row }: { registration: PilotRegistration; row: PilotProgramRow }) {
  const budgets = registration.budgets
  if (row.usage === null) {
    return (
      <p className="pilot-usage-unknown">
        Usage unavailable (<code>{row.usage_unavailable_reason}</code>). New jobs are refused until it is known.
      </p>
    )
  }
  const usage: BudgetUsage = row.usage
  return (
    <ul className="pilot-usage" aria-label={`Usage on ${usage.day}`}>
      <li>{`${usage.requests} of ${budgets.requests_per_day} requests`}</li>
      <li>{`${Math.ceil(usage.compute_seconds / 60)} of ${budgets.compute_minutes_per_day} compute minutes`}</li>
      <li>{`${bytes(usage.storage_bytes)} of ${budgets.storage_mb} MiB storage`}</li>
    </ul>
  )
}

function StateLine({ row }: { row: PilotProgramRow }) {
  if (row.state === 'unavailable') {
    return <p className="pilot-state pilot-state-unavailable">Unavailable: {blockerText(row.unavailable_reason)}</p>
  }
  const registration = row.registration
  if (registration.status === 'sealed') {
    return (
      <p className="pilot-state pilot-state-sealed">
        Sealed by {registration.sealed_by} at <time dateTime={registration.sealed_at}>{registration.sealed_at}</time>.
      </p>
    )
  }
  return <p className="pilot-state pilot-state-draft">Draft. Not sealed; it cannot run.</p>
}

function Blockers({ row }: { row: PilotProgramRow }) {
  if (row.run_blockers.length === 0) return <p>No blocker: a job of this program would be admitted now.</p>
  return (
    <ul className="pilot-blockers" aria-label="Why it cannot run now">
      {row.run_blockers.map((code) => (
        <li key={code}>{blockerText(code, row.registration)}</li>
      ))}
    </ul>
  )
}

function ProgramCard({ row }: { row: PilotProgramRow }) {
  const title = row.registration?.title ?? row.program_id
  return (
    <article className="card pilot-card" data-program={row.program_id} aria-labelledby={`pilot-${row.program_id}`}>
      <p className="eyebrow">{row.role ? ROLE_LABELS[row.role] : 'Registration unreadable'}</p>
      <h3 id={`pilot-${row.program_id}`}>
        <a href={link(row.program_id)}>{title}</a>
      </h3>
      <StateLine row={row} />
      {row.registration && <p>{row.registration.hypothesis}</p>}
      {row.registration && <UsageLines registration={row.registration} row={row} />}
      <Blockers row={row} />
    </article>
  )
}

function SealSection({ row, view, onSealed }: { row: PilotProgramRow; view: PilotView; onSealed: () => void }) {
  const [checked, setChecked] = useState(false)
  const [sending, setSending] = useState(false)
  const [answer, setAnswer] = useState<SealAnswer | null>(null)
  const [pending, setPending] = useState<SealCommandBody | null>(null)
  if (row.state !== 'draft') return null
  const unsealedControls = row.registration.controls
    .map((control) => control.program_id)
    .filter((id) => view.programs?.find((p) => p.program_id === id)?.state !== 'sealed')
  const sha = row.review_sha256
  // one seal command per page visit: the desk applies it, the view then shows it sealed
  const queued = answer?.outcome === 'queued'

  async function seal() {
    if (!checked || sending || queued || unsealedControls.length > 0) return
    // a retry after a network failure resends the same body (the store then
    // answers duplicate); anything else is a new command
    const reuse = pending && answer?.outcome === 'not_sent' && Date.parse(pending.expires_at) > Date.now()
    const body = reuse && pending ? pending : sealCommandBody(row.program_id, sha, crypto.randomUUID(), new Date())
    setPending(body)
    setSending(true)
    const result = await postSeal(body)
    setSending(false)
    setAnswer(result)
    if (result.outcome === 'queued') onSealed()
  }

  return (
    <section className="card pilot-seal" aria-labelledby="pilot-seal-title">
      <h3 id="pilot-seal-title">Seal this registration</h3>
      <p>
        Sealing is permanent and an owner act. The sealed registration is never edited; a change needs a new program id.
        Sealing does not start anything: jobs still pass the daily budgets, and every entry still needs your approval.
      </p>
      <p>
        Review hash (sha256 of the registration body): <code className="pilot-hash">{sha}</code>
      </p>
      {unsealedControls.map((id) => (
        <p key={id} role="note">
          Seal {id} first: a challenger is sealed against a sealed baseline.
        </p>
      ))}
      {!view.seal_command_available ? (
        <div role="note">
          <p>Sealing from the browser is not enabled on this server yet. From the loopback shell the owner runs:</p>
          <pre className="pilot-cli">
            python -m tree_options.research.pilot seal --program-id {row.program_id} --sha256 {sha} --owner-email
            &lt;your owner email&gt;
          </pre>
        </div>
      ) : (
        <>
          <label className="pilot-confirm">
            <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />I reviewed this
            registration and its hash
          </label>
          <button
            type="button"
            className="pilot-seal-button"
            disabled={!checked || sending || queued || unsealedControls.length > 0}
            onClick={() => void seal()}
          >
            Seal registration
          </button>
          <div role="status" aria-live="polite">
            {sending && <p>Sending the seal command…</p>}
            {answer?.outcome === 'queued' && (
              <p>
                Seal queued (command {answer.result.idempotency_key ?? pending?.idempotency_key}). The desk applies it; this
                page shows the sealed record once it has.
              </p>
            )}
            {answer?.outcome === 'refused' && (
              <p>
                Seal refused: <code>{answer.reason}</code> (HTTP {answer.httpStatus}).
              </p>
            )}
            {answer?.outcome === 'not_sent' && <p>Seal not sent ({answer.reason}). Nothing was queued; retry when online.</p>}
          </div>
        </>
      )}
    </section>
  )
}

function ProgramDetail({ row, view, refresh }: { row: PilotProgramRow; view: PilotView; refresh: () => void }) {
  const registration = row.registration
  return (
    <div className="pilot-detail">
      <p className="eyebrow">{row.role ? ROLE_LABELS[row.role] : 'Registration unreadable'}</p>
      <h2>{registration?.title ?? row.program_id}</h2>
      <StateLine row={row} />
      <Blockers row={row} />
      {registration && (
        <>
          <section className="card" aria-label="Hypothesis and decision rule">
            <h3>Hypothesis</h3>
            <p>{registration.hypothesis}</p>
            <h3>Decision rule</h3>
            <p>{registration.decision_rule.statement}</p>
            <dl className="pilot-facts">
              <div>
                <dt>Metric</dt>
                <dd>
                  <code>{registration.decision_rule.metric}</code>
                </dd>
              </div>
              <div>
                <dt>Minimum sessions</dt>
                <dd>{registration.decision_rule.minimum_sessions}</dd>
              </div>
              <div>
                <dt>Minimum completed lifecycles</dt>
                <dd>{registration.decision_rule.minimum_lifecycles}</dd>
              </div>
              <div>
                <dt>Universe</dt>
                <dd>
                  {registration.universe.asset_kind}: {registration.universe.symbols.join(', ')}
                </dd>
              </div>
              <div>
                <dt>Controls</dt>
                <dd>
                  {registration.controls.length === 0
                    ? 'None'
                    : registration.controls.map((c) => (
                        <a key={c.program_id} href={link(c.program_id)}>
                          {c.program_id} ({c.relation})
                        </a>
                      ))}
                </dd>
              </div>
              {registration.existing_registration && (
                <div>
                  <dt>Cited registration</dt>
                  <dd>
                    <code>{registration.existing_registration.path}</code> sha256{' '}
                    <code className="pilot-hash">{registration.existing_registration.sha256}</code>
                  </dd>
                </div>
              )}
            </dl>
          </section>
          <section className="card" aria-label="Data sources">
            <h3>Data sources and provenance</h3>
            <ul className="pilot-sources">
              {registration.data_sources.map((source) => (
                <li key={source.name}>
                  <p>
                    <strong>{source.name}</strong>: {source.provider}, {source.dataset} (
                    {source.realtime ? 'realtime' : 'not realtime'}; license {source.license})
                  </p>
                  <p className="muted">{source.provenance}</p>
                </li>
              ))}
            </ul>
          </section>
          <section className="card" aria-label="Budgets and usage">
            <h3>Daily budgets and today&apos;s usage</h3>
            <UsageLines registration={registration} row={row} />
            <p className="muted">
              Budgets are enforced when a job is admitted: one request per job, compute and stored bytes metered when it ends.
            </p>
          </section>
        </>
      )}
      {row.state === 'sealed' && <p>Sealed registrations are never edited; a change is a new program id.</p>}
      {row.state === 'draft' && <SealSection row={row} view={view} onSealed={refresh} />}
      <p className="muted">
        Review hash: <code className="pilot-hash">{row.review_sha256 ?? 'unavailable'}</code>
      </p>
    </div>
  )
}

/** The pilot content (no shell): the list, or one program's detail. */
export function PilotPanel({ programId }: { programId?: string }) {
  const poll = usePoll(getPilot, 30_000)
  const view = poll.data
  const row = programId ? view?.programs?.find((p) => p.program_id === programId) : undefined
  return (
    <div className="pilot-page">
      <p className="pilot-banner">
        Paper research only. This pilot never authorizes live trading, sends no order and grants no mandate; every entry
        still needs your approval.
      </p>
      {poll.error && (
        <p role="alert">
          {poll.error}
          {view && ' The last received registrations are shown below; current state is unavailable.'}
        </p>
      )}
      {!view && !poll.error && <p role="status">Loading the pilot…</p>}
      {programId && (
        <p>
          <a href="#/pilot">All pilot programs</a>
        </p>
      )}
      {view && view.status === 'unavailable' && (
        <p role="alert">
          Pilot registrations unavailable (<code>{view.unavailable_reason}</code>). Nothing can be sealed or run until they read.
        </p>
      )}
      {view && !view.owners_configured && (
        <p role="note">No owner list is configured on this server: nothing can be sealed, and sealed programs cannot run.</p>
      )}
      {view && view.status === 'ok' && (
        <>
          {view.roster_problems.length > 0 && (
            <section className="card" aria-label="Roster problems">
              <h3>The roster is incomplete</h3>
              <ul>
                {view.roster_problems.map((code) => (
                  <li key={code}>{rosterText(code)}</li>
                ))}
              </ul>
            </section>
          )}
          {programId ? (
            row ? (
              <ProgramDetail row={row} view={view} refresh={poll.refresh} />
            ) : (
              <p>Program {programId} is not in the pilot.</p>
            )
          ) : (
            <div className="pilot-list">
              {view.programs.map((p) => (
                <ProgramCard key={p.program_id} row={p} />
              ))}
            </div>
          )}
          <p className="muted">
            As of <time dateTime={view.now}>{view.now}</time>. Usage is per America/New_York day.
          </p>
        </>
      )}
    </div>
  )
}

/** #/pilot inside the current shell (lane E may render PilotPanel in its own). */
export function PilotPage({ programId }: { programId?: string }) {
  return (
    <AppShell
      title="Research pilot"
      showRuntimeBanners={false}
      contextLabel="trex · research pilot (paper)"
      footerSource="Registrations are sealed by an owner; budgets are enforced at job admission."
    >
      <PilotPanel programId={programId} />
    </AppShell>
  )
}
