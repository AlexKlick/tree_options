import { useEffect, useMemo, useState } from 'react'
import { getTradeFloorReplays } from '../lib/api'
import type { TradeFloorReplay } from '../lib/types'
import { usePoll } from '../hooks/usePoll'
import { AppShell } from './AppShell'

const MODEL_ORDER = ['zai', 'flash', 'minimax'] as const

function dollars(value: number | string): string {
  const n = Number(value)
  return Number.isFinite(n) ? new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', maximumFractionDigits: 2,
  }).format(n) : 'unavailable'
}

function signedDollars(value: string): string {
  const n = Number(value)
  return `${n > 0 ? '+' : ''}${dollars(n)}`
}

type Round = TradeFloorReplay['rounds'][number]

function scoreAt(rounds: Round[], revealedCount: number, startingCapital: number) {
  return Object.fromEntries(MODEL_ORDER.map((id) => {
    let capital = startingCapital
    let wins = 0
    let losses = 0
    let entries = 0
    for (const round of rounds.slice(0, revealedCount)) {
      const trader = round.traders.find((row) => row.id === id)
      if (trader?.action !== 'entered') continue
      entries += 1
      if (trader.eventual_pnl_proxy === null) continue
      const pnl = Number(trader.eventual_pnl_proxy)
      capital += pnl
      wins += Number(pnl > 0)
      losses += Number(pnl < 0)
    }
    return [id, { capital, wins, losses, entries }]
  })) as Record<(typeof MODEL_ORDER)[number], {
    capital: number; wins: number; losses: number; entries: number
  }>
}

export function TradeFloorPage() {
  const poll = usePoll(getTradeFloorReplays, 60_000)
  const [replayId, setReplayId] = useState('')
  const [windowId, setWindowId] = useState('')
  const [step, setStep] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1600)
  const safeList = poll.data?.execution_enabled === false ? poll.data.replays : []
  const game = safeList.find((row) => row.id === replayId) ?? safeList[0]
  const safeGame = game?.execution_enabled === false && game.research_only === true ? game : null
  const activeWindow = safeGame?.windows.find((row) => row.id === windowId) ?? safeGame?.windows[0]
  const rounds = useMemo(() => safeGame?.rounds.filter((row) => row.window === activeWindow?.id) ?? [],
    [safeGame, activeWindow?.id])
  const maxStep = Math.max(0, rounds.length * 2 - 1)
  const currentStep = Math.min(step, maxStep)
  const roundIndex = Math.floor(currentStep / 2)
  const round = rounds[roundIndex]
  const revealed = currentStep % 2 === 1
  const revealedCount = Math.floor((currentStep + 1) / 2)
  const score = useMemo(() => scoreAt(rounds, revealedCount, Number(safeGame?.starting_capital ?? 5000)),
    [rounds, revealedCount, safeGame?.starting_capital])

  useEffect(() => {
    if (!playing || rounds.length === 0) return
    const timer = window.setInterval(() => setStep((value) => Math.min(value + 1, maxStep)), speed)
    return () => window.clearInterval(timer)
  }, [playing, rounds.length, maxStep, speed])
  useEffect(() => { if (currentStep >= maxStep) setPlaying(false) }, [currentStep, maxStep])

  return (
    <AppShell title="Trade floor" poll={poll} showRuntimeBanners={false}
      footerSource="Broker-free · validated historical replay · $5,000 virtual capital">
      <section className="trade-floor-hero" aria-label="Virtual trade floor">
        <div>
          <div className="eyebrow">Historical game · spectator mode</div>
          <h2>Three model traders. One market clock.</h2>
          <p>Watch Z.ai, Z.ai Flash, and MiniMax choose from the same historical option-spread board. Reveal the later modeled mark after each round.</p>
        </div>
        <div className="trade-floor-live-mark"><span aria-hidden="true">◉</span> Research replay<br /><small>No broker connection</small></div>
      </section>

      {poll.error && <p className="card" role="alert">Trade-floor replay unavailable: {poll.error}</p>}
      {!poll.error && poll.data && safeList.length === 0 && <p className="card">No validated trade-floor replay is published yet.</p>}
      {poll.data && poll.data.execution_enabled !== false && <p className="card" role="alert">Trade-floor authority state is invalid.</p>}
      {safeGame && activeWindow && round && <>
        <section className="trade-floor-controls card" aria-label="Replay controls">
          <div className="trade-floor-pickers">
            {safeList.length > 1 && <label>Run
              <select value={safeGame.id} onChange={(event) => { setReplayId(event.target.value); setWindowId(''); setStep(0); setPlaying(false) }}>
                {safeList.map((item) => <option key={item.id} value={item.id}>{item.id}</option>)}
              </select>
            </label>}
            <div className="trade-floor-window-tabs" role="group" aria-label="Historical window">
              {safeGame.windows.map((item) => <button type="button" key={item.id}
                aria-pressed={item.id === activeWindow.id}
                onClick={() => { setWindowId(item.id); setStep(0); setPlaying(false) }}>{item.id.toUpperCase()}<small>{item.start} to {item.end}</small></button>)}
            </div>
          </div>
          <div className="trade-floor-playback">
            <button type="button" onClick={() => { setStep(0); setPlaying(false) }} disabled={currentStep === 0}>Reset</button>
            <button type="button" onClick={() => { setStep(Math.max(0, currentStep - 1)); setPlaying(false) }} disabled={currentStep === 0}>Back</button>
            <button type="button" className="trade-floor-primary" onClick={() => setPlaying((value) => !value)} disabled={currentStep >= maxStep}>{playing ? 'Pause' : 'Play'}</button>
            <button type="button" onClick={() => { setStep(Math.min(maxStep, currentStep + 1)); setPlaying(false) }} disabled={currentStep >= maxStep}>{revealed ? 'Next round' : 'Reveal outcome'}</button>
            <label>Speed <select value={speed} onChange={(event) => setSpeed(Number(event.target.value))}>
              <option value={2400}>Slow</option><option value={1600}>Normal</option><option value={800}>Fast</option>
            </select></label>
          </div>
          <p className="trade-floor-progress">Round {roundIndex + 1} of {rounds.length} · {revealed ? 'later mark revealed' : 'decision on the floor'} · {round.snapshot_id.slice(2)} ET</p>
          <progress value={currentStep + 1} max={maxStep + 1} aria-label="Replay progress" />
        </section>

        <section className="trade-floor-tape" aria-label="Market tape">
          <span className="trade-floor-tape-label">MARKET TAPE</span>
          <span>{activeWindow.series} captured series</span>
          <span>{activeWindow.traded_minute_bars.toLocaleString()} traded-minute bars</span>
          <span>{round.all_as_of_candidates} available spreads</span>
          <span>{round.candidates.length} shown to each model</span>
        </section>

        <section className="trade-floor-seats" aria-label="Model traders">
          {MODEL_ORDER.map((id, index) => {
            const trader = round.traders.find((item) => item.id === id)
            const chosen = round.candidates.find((item) => item.id === trader?.selected_id)
            const account = score[id]
            return <article className={`trade-floor-seat trade-floor-seat-${id}`} key={id}>
              <div className="trade-floor-seat-top"><span className="trade-floor-avatar" aria-hidden="true">{index + 1}</span><div><div className="eyebrow">Trader seat {index + 1}</div><h3>{trader?.label ?? id}</h3></div></div>
              <div className="trade-floor-capital">{dollars(account.capital)}<small>virtual closed capital</small></div>
              <div className="trade-floor-record">{account.entries} entered · {account.wins} modeled wins · {account.losses} modeled losses</div>
              <div className="trade-floor-choice">
                <span className="eyebrow">This round's call</span>
                <strong>{chosen ? `${chosen.symbol} ${chosen.structure.replace('_', ' ')}` : 'Stand aside'}</strong>
                <span>{chosen ? `Observed premium ${dollars(Number(chosen.premium_proxy) * 100)} · risk proxy ${dollars(chosen.max_loss_proxy)}` : round.candidates.length === 0 ? 'No eligible spread on this board' : 'No spread selected'}</span>
              </div>
              {trader?.model_reason && <p className="trade-floor-reason">“{trader.model_reason}”</p>}
              <div className={`trade-floor-outcome ${revealed ? 'trade-floor-outcome-shown' : ''}`} aria-live="polite">
                {revealed ? trader?.action === 'entered' ? <>
                  <strong className={Number(trader.eventual_pnl_proxy) >= 0 ? 'trade-floor-positive' : 'trade-floor-negative'}>{trader.eventual_pnl_proxy === null ? 'Still open' : signedDollars(trader.eventual_pnl_proxy)}</strong>
                  <span>later trade-bar mark · modeled risk {dollars(trader.entry_loss_proxy ?? '0')}</span>
                </> : <><strong>{trader?.action === 'blocked' ? 'Entry blocked' : 'No trade'}</strong><span>{trader?.action_reason.replace(/_/g, ' ')}</span></>
                  : <><strong>Outcome hidden</strong><span>Reveal after the decisions</span></>}
              </div>
            </article>
          })}
        </section>

        <section className="card trade-floor-board" aria-label="Potential trade board">
          <div className="card-head"><div><div className="eyebrow">As-of opportunity graph</div><h2>Candidate board</h2></div><span className="muted">Observed trade prices · not executable quotes</span></div>
          {round.candidates.length === 0 ? <p>No candidate passed the historical as-of filters at this decision point. All three traders sat out.</p> :
            <div className="trade-floor-candidates">{round.candidates.map((candidate) => {
              const selectedBy = round.traders.filter((trader) => trader.selected_id === candidate.id).map((trader) => trader.label)
              return <div className={`trade-floor-candidate ${selectedBy.length ? 'trade-floor-candidate-picked' : ''}`} key={candidate.id}>
                <div><strong>{candidate.symbol}</strong><span>{candidate.structure.replace('_', ' ')} · {candidate.dte} DTE</span></div>
                <div className="trade-floor-candidate-numbers"><span>risk {dollars(candidate.max_loss_proxy)}</span><span>gain {dollars(candidate.max_gain_proxy)}</span><span>R:R {candidate.reward_to_risk_proxy}</span></div>
                <small>{selectedBy.length ? `Picked by ${selectedBy.join(', ')}` : 'Unselected'} · {candidate.id.slice(0, 8)}</small>
              </div>
            })}</div>}
        </section>

        <section className="card trade-floor-evidence" aria-label="Replay evidence and limits">
          <div className="eyebrow">Evidence desk</div>
          <h2>What the floor is replaying</h2>
          <p>{safeGame.rounds.length} comparable rounds across {safeGame.windows.length} disjoint windows. Calibration round {safeGame.excluded_calibration_snapshot} is excluded from every trader score. Each window starts again at {dollars(safeGame.starting_capital)} virtual capital.</p>
          <p className="muted">Run {safeGame.id} · source {safeGame.source_head.slice(0, 12)} · receipt {safeGame.replay_manifest_sha256.slice(0, 12)} · execution disabled.</p>
          <ul>{safeGame.limitations.map((limit) => <li key={limit}>{limit}</li>)}</ul>
        </section>
      </>}
    </AppShell>
  )
}
