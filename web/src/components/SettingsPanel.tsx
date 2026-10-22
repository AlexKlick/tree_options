// Settings (#/activity/settings): read-only profile facts, the current state
// of the stops, and what each control does. The controls themselves live in
// the control bar (lane E); this panel explains them and repeats none.

import { Fragment } from 'react'
import { usePoll } from '../hooks/usePoll'
import { getToday, onboardingSteps, reasonText, type OnboardingStep } from '../lib/activityApi'
import {
  ACCEPTANCE_THRESHOLDS,
  COMMAND_MAX_TTL_S,
  CONTROL_LABELS,
  DRILL_IDS,
  PRIMARY_CONTROL,
  isKnown,
  type KillSwitchState,
  type PortfolioView,
  type TodayView,
} from '../lib/contracts'
import { etDateTime } from '../lib/format'
import { getPortfolio } from '../lib/portfolioApi'
import { Section } from './PortfolioPage'
import './accountPages.css'

const STEP_WORDS: Record<OnboardingStep['state'], string> = { done: 'Done', todo: 'To do', unknown: 'Unknown' }

function stepText(step: OnboardingStep | undefined): string {
  return step ? `${STEP_WORDS[step.state]}: ${step.detail}` : 'Unknown'
}

function AccountFact({ portfolio, error }: { portfolio: PortfolioView | null; error: string | null }) {
  if (portfolio === null) {
    return (
      <dd className="acct-unavailable">
        {error ? `Unavailable: the portfolio could not be read (${error}).` : 'Loading…'}
      </dd>
    )
  }
  if (portfolio.account_ids.length === 0) {
    return <dd className="acct-unavailable">Unavailable: no account is recorded in the books yet.</dd>
  }
  return <dd>{portfolio.account_ids.join(', ')}</dd>
}

function ProfileSection({
  today,
  todayError,
  portfolio,
  portfolioError,
}: {
  today: TodayView | null
  todayError: string | null
  portfolio: PortfolioView | null
  portfolioError: string | null
}) {
  const steps = today === null && todayError === null ? null : onboardingSteps(today, todayError)
  const step = (id: OnboardingStep['id']) => steps?.find((s) => s.id === id)
  return (
    <Section title="Profile" id="settings-profile">
      <p className="muted">Read-only. The owner changes these on the host, never from this page.</p>
      <dl className="kv">
        <dt>Paper account</dt>
        <AccountFact portfolio={portfolio} error={portfolioError} />
        <dt>Capital profile</dt>
        <dd>{steps ? stepText(step('profile')) : 'Loading…'}</dd>
        <dt>Loss limits</dt>
        <dd className="acct-unavailable">
          Unavailable: this version has no read-only view of the profile's limits. The desk checks every entry against
          them before it proposes it.
        </dd>
        <dt>Exit watch</dt>
        <dd>
          {today
            ? `The desk owner's heartbeat counts as stale after ${today.desk_owner.stale_after_s} s.`
            : 'Unknown until Today is read.'}
        </dd>
        <dt>Command lifetime</dt>
        <dd>
          A command expires {COMMAND_MAX_TTL_S} s after you send it; an approval also ends at the proposal's deadline.
        </dd>
        <dt>Phone commands</dt>
        <dd>{steps ? stepText(step('family_key')) : 'Loading…'}</dd>
        <dt>Live trading</dt>
        <dd>Off. Paper only; paper acceptance never unlocks live trading.</dd>
      </dl>
    </Section>
  )
}

function killSwitchText(state: KillSwitchState): string {
  if (state.status === 'clear') return 'Clear'
  if (state.status === 'standing') return `Standing since ${etDateTime(state.since)}${state.reason ? `: ${state.reason}` : ''}`
  return `Unavailable: ${reasonText(state.unavailable_reason)}`
}

function CurrentStateSection({ today }: { today: TodayView | null }) {
  const controls = today?.controls
  return (
    <Section title="Current state" id="settings-state">
      {!controls ? (
        <p className="acct-unavailable">Unknown until Today is read.</p>
      ) : (
        <dl className="kv">
          <dt>{CONTROL_LABELS.pause_entries}</dt>
          <dd>{killSwitchText(controls.pause_entries)}</dd>
          <dt>{CONTROL_LABELS.halt}</dt>
          <dd>{killSwitchText(controls.halt)}</dd>
          <dt>{CONTROL_LABELS.flatten}</dt>
          <dd>{killSwitchText(controls.flatten)}</dd>
          <dt>Waiting for the desk</dt>
          <dd>
            {isKnown(controls.queued)
              ? controls.queued.items.length === 0
                ? 'No command queued.'
                : `${controls.queued.items.length} command${controls.queued.items.length === 1 ? '' : 's'} queued: ${controls.queued.items
                    .map((item) => item.kind.replace(/_/g, ' '))
                    .join(', ')}.`
              : `Unavailable: ${reasonText(controls.queued.unavailable_reason)}`}
          </dd>
        </dl>
      )}
    </Section>
  )
}

/** What each control does, in the order the control bar offers them. */
export const CONTROL_EXPLANATIONS: { kind: keyof typeof CONTROL_LABELS; text: string }[] = [
  {
    kind: PRIMARY_CONTROL,
    text:
      'Primary control. Stops the desk from opening new positions. It never stops exits: open positions keep ' +
      'their stops, targets and deadlines, and fills and reconciliation go on. Use it whenever you are unsure.',
  },
  { kind: 'resume_entries', text: 'Lets the desk open new entries again. Every entry still needs your approval.' },
  {
    kind: 'halt',
    text:
      'Stops everything, exits included: open positions are not watched until you clear it. Use it only when the ' +
      'desk itself misbehaves; otherwise pause new entries.',
  },
  {
    kind: 'flatten',
    text:
      'Closes every position the desk owns, options and stocks, and blocks new entries while it stands. It never ' +
      'touches positions outside TREX.',
  },
  {
    kind: 'resume_all',
    text:
      'Removes the emergency stop, close-all and the pause together. It does not bring back earlier approvals, and ' +
      'it never lifts a stop sent after it.',
  },
]

function ControlsSection() {
  return (
    <Section title="Controls" id="settings-controls">
      <dl className="acct-controls">
        {CONTROL_EXPLANATIONS.map(({ kind, text }) => (
          <Fragment key={kind}>
            <dt>{CONTROL_LABELS[kind]}</dt>
            <dd>{text}</dd>
          </Fragment>
        ))}
      </dl>
      <p>
        This page explains the controls and repeats none of them. Use the control bar at the top of every page, or{' '}
        <a href="#/today">open Today</a>. A command from the phone is queued and applied by the desk; Activity shows
        when it was applied.
      </p>
    </Section>
  )
}

function AcceptanceRulesSection() {
  const t = ACCEPTANCE_THRESHOLDS
  return (
    <Section title="Acceptance rules" id="settings-acceptance">
      <ul>
        <li>
          {t.streak_sessions} consecutive qualifying sessions. A session qualifies when:
          <ul>
            <li>the desk heartbeat was fresh for at least {t.heartbeat_min_percent}% of regular hours;</li>
            <li>
              the books matched the broker at the close, and a mismatch at the open was taken up within{' '}
              {t.open_check_window_s / 60} minutes;
            </li>
            <li>the broker had no order TREX does not own;</li>
            <li>
              no open position went unwatched for more than {t.max_exit_unguarded_s} seconds (a standing emergency
              stop counts as unwatched).
            </li>
          </ul>
        </li>
        <li>
          {t.lifecycles_per_kind} completed paper trades of each kind (options and stocks) inside the streak.
        </li>
        <li>All {DRILL_IDS.length} drills passed, with evidence, attested by an owner.</li>
      </ul>
      <p>
        Meeting them never unlocks live trading. <a href="#/activity/acceptance">See the progress</a>.
      </p>
    </Section>
  )
}

export function SettingsPanel() {
  const today = usePoll(getToday, 30_000)
  const portfolio = usePoll(getPortfolio, 60_000)
  return (
    <div className="acct-page">
      {today.error && (
        <p role="alert" className="acct-alert">
          Today could not be read: {today.error}. The explanations below still apply; the current state is unknown.
        </p>
      )}
      <ProfileSection
        today={today.data}
        todayError={today.error}
        portfolio={portfolio.data}
        portfolioError={portfolio.error}
      />
      <CurrentStateSection today={today.data} />
      <ControlsSection />
      <AcceptanceRulesSection />
    </div>
  )
}
