import { useState } from 'react'
import { getActionModelExample, getAutomation, getDeskStandings, getHistoricalReplays, getIntradayGraphs, getLabScoreboard, getLongRun, getPlans, getPortfolioScenarios, getSupervisedDesk, postAutomationAction, postSupervisedControl } from '../lib/api'
import type { ActionNode, DeskStandings, LabScoreboard, LongRunDigest, LongRunPaired, SupervisedDeskStatus } from '../lib/types'
import { usePoll } from '../hooks/usePoll'
import { AppShell } from './AppShell'
import { AccountExposureBanner } from './AccountExposureBanner'
import { Pill } from './Pill'

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

function AutomationCard() {
  const automation = usePoll(getAutomation, 30_000)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const act = async (key: string, action: 'enable' | 'disable' | 'run') => {
    setBusy(`${key}/${action}`)
    setError(null)
    try {
      await postAutomationAction(key, action)
      await automation.refresh?.()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }

  const control = async (action: 'halt' | 'flatten' | 'resume') => {
    setBusy(`supervised/${action}`)
    setError(null)
    try {
      await postSupervisedControl(action)
      await automation.refresh?.()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="card" aria-label="Desk automation settings and controls">
      <div className="eyebrow">Desk automation · operator controls</div>
      <h2>Automation</h2>
      <p className="muted">The desk's own timers, plus the supervised desk's stop files. Every action goes through a fixed unit whitelist and is audited to automation.jsonl; nothing here contacts the broker.</p>
      {automation.error && <p role="alert">Automation status unavailable: {automation.error}</p>}
      {error && <p role="alert">{error}</p>}
      {automation.data && (
        <>
          <p>
            Stop files:{' '}
            {automation.data.kill_files.length > 0
              ? automation.data.kill_files.join(', ')
              : 'none — the desk is live'}{' '}
            <button type="button" onClick={() => control('halt')} disabled={busy !== null}>HALT</button>{' '}
            <button type="button" onClick={() => control('flatten')} disabled={busy !== null}>FLATTEN</button>{' '}
            <button type="button" onClick={() => control('resume')} disabled={busy !== null}>Resume</button>
          </p>
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th scope="col">Timer</th><th scope="col">State</th><th scope="col">Next fire</th><th scope="col">Last run</th><th scope="col">Controls</th></tr>
              </thead>
              <tbody>
                {automation.data.timers.map((t) => (
                  <tr key={t.key} data-testid={`timer-${t.key}`}>
                    <th scope="row">{t.key}</th>
                    <td>{t.enabled ? 'enabled' : 'disabled'}{t.active ? ' · active' : ''}</td>
                    <td>{t.next_elapse || '—'}</td>
                    <td>{t.last_result || 'never'}{t.last_exit ? ` (${t.last_exit})` : ''}</td>
                    <td>
                      <button type="button" onClick={() => act(t.key, t.enabled ? 'disable' : 'enable')} disabled={busy !== null}>
                        {t.enabled ? 'Disable' : 'Enable'}
                      </button>{' '}
                      <button type="button" onClick={() => act(t.key, 'run')} disabled={busy !== null}>Run now</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}

const money = (value: number) => `${value < 0 ? '−' : '+'}$${Math.abs(value).toFixed(2)}`
const ciText = (ci: [number, number]) => `[${money(ci[0])}, ${money(ci[1])}]`
const pairText = (pair: LongRunPaired | null | undefined) =>
  pair ? `${money(pair.diff_total)} ${ciText(pair.ci95)}` : '—'
const ownPairText = (own: (LongRunPaired & { p_enter: number }) | null | undefined) =>
  own ? `${pairText(own)} · p_enter ${(own.p_enter * 100).toFixed(1)}%` : '—'
const duration = (seconds: number | null) =>
  seconds === null ? 'unknown'
    : seconds < 90 ? `${Math.round(seconds)} s`
      : seconds < 5400 ? `${Math.round(seconds / 60)} min` : `${(seconds / 3600).toFixed(1)} h`

function LongRunDigestBlock({ digest }: { digest: LongRunDigest }) {
  const { aa, random_null: rn, walk_forward: wf } = digest
  return (
    <>
      <p><strong>{digest.headline}</strong></p>
      <p data-testid="longrun-aa">
        {aa.status === 'not_run'
          ? `A/A not run — ${aa.reason ?? 'no incumbent pair'}; every comparison is unvalidated.`
          : `A/A ${aa.status === 'valid' ? 'valid' : 'INVALID'}: ${(aa.pair ?? []).join(' vs ')} agree on ${aa.agreement === undefined ? '?' : (aa.agreement * 100).toFixed(1)}% of ${aa.boards ?? '?'} boards · diff ${pairText(aa.diff)}`}
      </p>
      <p data-testid="longrun-random-band">Random picker at the incumbent's entry rate ({(rn.p_enter * 100).toFixed(1)}%, {rn.seeds} seeds on the same boards): expected {money(rn.expected_total)} {ciText(rn.expected_ci95)} · 95% null band {ciText(rn.band95)}</p>
      <p className="muted">{digest.boards.scored} of {digest.boards.total} boards scored ({digest.boards.excluded} excluded for a missing or failed decision in some arm) · {digest.boards.sessions.count} sessions. Totals are net modeled dollars with session-bootstrap 95% ranges.</p>
      <div className="table-scroll">
        <table>
          <thead>
            <tr><th scope="col">Arm</th><th scope="col">Kind</th><th scope="col">Entered</th><th scope="col">Failed</th><th scope="col">Net total [95% CI]</th><th scope="col" title="Paired diff vs a random null matched to THIS arm's own entry rate (p_enter is that matched rate) — the skill comparison.">vs random (own entry rate)</th><th scope="col" title="Legacy: one shared random null at the pooled entry rate, so this column is each arm's net total minus a single shared constant — NOT a skill measure. Retained for continuity only; never ranked on.">vs random (legacy)</th><th scope="col">vs incumbent</th><th scope="col">vs bullish regime</th></tr>
          </thead>
          <tbody>
            {digest.standings.map((row) => (
              <tr key={row.arm} data-testid={`longrun-standing-${row.arm}`}>
                <th scope="row">{row.arm}</th>
                <td>{row.kind}</td>
                <td>{row.entered}{row.unevaluable > 0 ? ` (${row.unevaluable} no fill)` : ''}</td>
                <td>{row.failures}</td>
                <td>{money(row.net_total)} {ciText(row.net_ci95)}</td>
                <td>{ownPairText(row.vs_random_own)}</td>
                <td title="Legacy shared-null column — not a skill measure (see the column header).">{pairText(row.vs_random)}</td>
                <td>{pairText(row.vs_incumbent)}</td>
                <td>{pairText(row.vs_regime)}</td>
              </tr>
            ))}
            <tr data-testid="longrun-random">
              <th scope="row">random (matched)</th><td>control</td><td>—</td><td>—</td>
              <td>{money(rn.expected_total)} {ciText(rn.expected_ci95)}</td><td>—</td><td>—</td><td>—</td>
            </tr>
            {digest.benchmarks.map((bench) => bench.status === 'ok' && bench.net_total !== undefined && bench.net_ci95 ? (
              <tr key={bench.name} data-testid={`longrun-benchmark-${bench.name}`}>
                <th scope="row">{bench.name} buy-and-hold</th><td>benchmark</td><td>—</td><td>—</td>
                <td>{money(bench.net_total)} {ciText(bench.net_ci95)}</td><td>—</td><td>—</td><td>—</td>
              </tr>
            ) : null)}
          </tbody>
        </table>
      </div>
      {digest.skill && Object.keys(digest.skill.arms).length > 0 && (
        <p data-testid="longrun-skill">Skill (exact counterfactual, descriptive — never promotes): {Object.entries(digest.skill.arms).map(([name, a]) => `${name}: ${a.verdict}${(a.boards_dropped_unpriced ?? 0) > 0 ? ` — ${a.boards_dropped_unpriced} boards dropped unpriced` : ''}`).join(' · ')}</p>
      )}
      {(digest.skill?.no_price?.total ?? 0) > 0 && digest.skill?.no_price && (
        <p data-testid="longrun-skill-no-price">NO PRICE: {digest.skill.no_price.total} boards dropped unpriced ({Object.entries(digest.skill.no_price.by_arm).map(([a, n]) => `${a} ${n}`).join(', ')}; reasons: {Object.entries(digest.skill.no_price.by_reason).map(([r, n]) => `${r} ${n}`).join(', ')}). Totals exclude them — a run the ledger refused to price is not a break-even result.</p>
      )}
      {digest.skill && Object.values(digest.skill.arms).some((a) => a.cost_provenance != null) && (
        <p data-testid="longrun-skill-cost" className="muted">
          {(() => {
            const cp = Object.values(digest.skill!.arms).find((a) => a.cost_provenance)!.cost_provenance!
            return `Cost basis: ${cp.source}${cp.snapshot_window_et ? ` (snapshots ${cp.snapshot_window_et} ET)` : ''}${cp.describes_fill_clock === false ? ' — these costs do not describe the fill clock' : ''}${cp.gap ? `: ${cp.gap}` : '.'}`
          })()}
        </p>
      )}
      {wf.status === 'ok'
        ? <p data-testid="longrun-wf">Walk-forward (cutoff {wf.cutoff}, {wf.tune_sessions} tune / {wf.test_sessions} test sessions, at most {wf.max_finalists} finalists tested once): {wf.finalists.length === 0 ? 'no finalists.' : wf.finalists.map((f) => `${f.policy} test ${money(f.test.net_total)} ${ciText(f.test.net_ci95)}, vs random ${pairText(f.test.vs_random)}, Holm p ${f.holm_p.toFixed(3)}${f.test_entries != null && wf.min_test_entries != null ? `, ${f.test_entries}/${wf.min_test_entries} evaluated test entries` : ''}${f.eligible_for_operator_review ? ' — eligible for operator review' : ''}`).join('; ')}</p>
        : <p className="muted">Walk-forward not applicable{wf.reason ? ` — ${wf.reason}` : ''}.</p>}
      <p className="muted">Never promoted: the pre-registered rule is text for the operator. {digest.promotion.rule}</p>
    </>
  )
}

function LongRunCard() {
  const longrun = usePoll(getLongRun, 30_000)
  const view = longrun.data
  const progress = view?.progress
  return (
    <section className="card" aria-label="Desk long run">
      <div className="eyebrow">Desk lab · long run · evidence only</div>
      <h2>Long run</h2>
      <p className="muted">{view?.digest?.untrusted_note ?? 'Every policy decides on the same boards; totals carry session-bootstrap 95% ranges, never bare point totals. Nothing here is ever promoted — promotion stays the operator\'s pre-registered-rule decision.'}</p>
      {longrun.error && <p role="alert">Long run unavailable: {longrun.error}</p>}
      {view && view.run === null && <p>No long run has started in this cockpit store.</p>}
      {view?.run && progress && (
        <>
          <p>Run <code>{view.run}</code> · {progress.status} · {progress.finished ?? 0}/{progress.total ?? 0} decisions · {progress.failures ?? 0} failed · paused {duration(progress.paused_s ?? 0)} · ETA {duration(progress.eta_s)}{progress.quota ? ` · quota: ${progress.quota.reason}` : ''}{progress.legacy ? ` · prototype (${progress.legacy}) progress` : ''}</p>
          <ul>
            {Object.entries(progress.arms).map(([name, arm]) => (
              <li key={name} data-testid={`longrun-arm-${name}`}>
                <progress value={arm.done ?? 0} max={arm.total ?? 1} aria-label={`${name} progress`} />{' '}
                {name} ({arm.kind}): {arm.done ?? 0}/{arm.total ?? '?'} · {arm.entered ?? 0} entered · {arm.failures ?? 0} failed{arm.net !== null ? ` · running isolated net ${money(arm.net)}` : ''}
              </li>
            ))}
          </ul>
        </>
      )}
      {view?.digest && <LongRunDigestBlock digest={view.digest} />}
    </section>
  )
}

/** A closed-pnl decimal string ("17", "-3.5") as the deck's money shape. */
const decimalMoney = (value: string) => {
  const n = Number(value)
  return Number.isFinite(n) ? money(n) : value
}

function StandingsCard() {
  const standings = usePoll(getDeskStandings, 60_000)
  const view: DeskStandings | null = standings.data
  return (
    <section className="card" aria-label="Desk challenge rule standings">
      <div className="eyebrow">Desk challenge · sealed rule · evidence only</div>
      <h2>Rule standings</h2>
      {standings.error && <p role="alert">Rule standings unavailable: {standings.error}</p>}
      {view && (
        <>
          <p className="muted">
            Cross-digest standings over {view.games_counted} post-seal game
            {view.games_counted === 1 ? '' : 's'} — every number is mechanical
            replay accounting from the digests; the sealed rule&apos;s clauses
            are the operator&apos;s to read on them.
          </p>
          <div className="pill-row" style={{ marginBottom: 10 }}>
            <Pill variant="empty">cost baseline ${view.cost_baseline_per_game.toFixed(2)} / game</Pill>
            <Pill variant="empty">registration sample through {view.registration_sample_through}</Pill>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th scope="col">Policy</th><th scope="col">Kind</th><th scope="col">Games</th><th scope="col">Boards</th><th scope="col">Closed pnl</th><th scope="col">Sessions</th></tr>
              </thead>
              <tbody>
                {view.policies.map((row) => (
                  <tr key={row.policy} data-testid={`standings-row-${row.policy}`}>
                    <th scope="row">{row.policy}</th>
                    <td>{row.kind}</td>
                    <td>{row.games}</td>
                    <td>{row.boards}</td>
                    <td>{decimalMoney(row.closed_pnl_sum)}</td>
                    <td>{row.sessions_distinct}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {view.policies.length === 0 && <p className="muted">No post-seal challenge game has completed yet.</p>}
          <p className="muted" data-testid="standings-untrusted-note">{view.untrusted_note}</p>
        </>
      )}
    </section>
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
      <AutomationCard />
      <LongRunCard />
      <StandingsCard />
      <section className="card" aria-label="Supervised paper desk">
        <div className="eyebrow">Existing TREX paper system · local observations</div>
        <h2>Supervised desk (paper)</h2>
        <p>Governed entry runs on the supervised desk — a separate armed path with its own operator mandate, effect permits, and IBKR paper broker session. This cockpit only reads the desk's on-disk state; it never contacts the broker. The synthetic action-model plan on this page remains unhooked: no mandate, permit, or dispatch path is installed for it.</p>
        {desk.error && <p role="alert">Supervised desk status unavailable: {desk.error}</p>}
        {desk.data && <AccountExposureBanner exposure={desk.data.account_exposure} />}
        {desk.data && <DeskStatusLines status={desk.data} />}
        {lab.error && <p role="alert">Lab scoreboard unavailable: {lab.error}</p>}
        {lab.data && <LabScoreboardBlock board={lab.data} />}
        {accountPoll.data?.account ? (
          <p>Broker account snapshot: <code>{accountPoll.data.account.account_id}</code> · observed {accountPoll.data.account.ts} · age {accountPoll.data.account.age_seconds ?? 'unknown'} seconds. Snapshot equity does not set a strategy budget.</p>
        ) : (
          <p className="muted">Broker account snapshot unavailable in this view.</p>
        )}
        {accountPoll.data?.net_positions && <p>Existing TREX positions: {accountPoll.data.net_positions.length} underlying rows{desk.data?.account_exposure && <> ({desk.data.account_exposure.outside_desk_book.structures} structure{desk.data.account_exposure.outside_desk_book.structures === 1 ? '' : 's'}, {desk.data.account_exposure.outside_desk_book.legs ?? 'unknown'} option legs, {desk.data.account_exposure.max_loss_usd === null ? 'uncountable' : `$${desk.data.account_exposure.max_loss_usd}`} max loss)</>}. Their legacy monitor remains the owner; this action model does not claim or control them. {accountPoll.data.net_positions.length > 0 && 'The supervised canary blocks an entry only on the candidate’s own underlying (Ruling 3b), so a candidate on another underlying is admitted into this non-flat account; their open loss still counts against the account open-loss budget.'}</p>}
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
