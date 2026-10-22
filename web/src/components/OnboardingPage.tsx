// Getting started (#/onboarding): the first-run checklist, read from one
// GET /api/today (trex-today/1). A step whose fact Today does not show is
// "Unknown" with the reason, never "Done".

import { usePoll } from '../hooks/usePoll'
import { getToday, onboardingSteps, type OnboardingStep } from '../lib/activityApi'
import { AppShell } from './AppShell'
import { StatusPill, type Tone } from './PortfolioPage'
import './accountPages.css'

const STATE_PILL: Record<OnboardingStep['state'], [Tone, string]> = {
  done: ['ok', 'Done'],
  todo: ['warn', 'To do'],
  unknown: ['unknown', 'Unknown'],
}

function linkLabel(href: string): string {
  if (href.startsWith('#/trade-review/')) return 'Review the waiting proposal'
  return href.startsWith('#/activity') ? 'Open Activity' : 'Open Today'
}

/** `3 of 5 steps done` (plus `, N unknown` when some are unknown). */
export function progressText(steps: OnboardingStep[]): string {
  const done = steps.filter((s) => s.state === 'done').length
  const unknown = steps.filter((s) => s.state === 'unknown').length
  return `${done} of ${steps.length} steps done${unknown > 0 ? `, ${unknown} unknown` : ''}`
}

function StepCard({ step }: { step: OnboardingStep }) {
  const [tone, word] = STATE_PILL[step.state]
  return (
    <li className="card acct-step">
      <div className="acct-head">
        <h2>{step.title}</h2>
        <StatusPill tone={tone}>{word}</StatusPill>
      </div>
      <p>{step.detail}</p>
      {step.next && (
        <p>
          <strong>Next:</strong> {step.next}
        </p>
      )}
      {step.href && (
        <p>
          <a href={step.href}>{linkLabel(step.href)}</a>
        </p>
      )}
    </li>
  )
}

export function OnboardingPage() {
  const poll = usePoll(getToday, 30_000)
  const answered = poll.data !== null || poll.error !== null
  const steps = answered ? onboardingSteps(poll.data, poll.data === null ? poll.error : null) : null
  return (
    <AppShell
      title="Getting started"
      poll={poll}
      showRuntimeBanners={false}
      contextLabel="trex · first run"
      footerSource="Read from Today. This page changes nothing on the desk."
    >
      <div className="acct-page">
        <p className="acct-intro">
          Five checks before your first paper trade. TREX trades a paper account only, you approve every entry, and
          exits run on their own.
        </p>
        {poll.error && (
          <p role="alert" className="acct-alert">
            Today could not be read: {poll.error}.{poll.data ? ' Showing the last state received.' : ''}
          </p>
        )}
        {!answered && <p role="status">Loading Today…</p>}
        {steps && (
          <>
            <p className="acct-meter">{progressText(steps)}</p>
            <ol className="acct-steps" aria-label="First-run checklist">
              {steps.map((step) => (
                <StepCard key={step.id} step={step} />
              ))}
            </ol>
            <p className="muted">
              The user guide (docs/product/USER-GUIDE.md) explains each step, approving from the phone, and the
              difference between pausing entries and the emergency stop. When every step is done,{' '}
              <a href="#/today">go to Today</a>.
            </p>
          </>
        )}
      </div>
    </AppShell>
  )
}
