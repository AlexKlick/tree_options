import { describe, expect, it } from 'vitest'
import { ACCEPTANCE_THRESHOLDS, CONTROL_LABELS, DRILL_IDS } from './contracts'
import { DRILL_LABELS } from './activityApi'

// docs/product/USER-GUIDE.md and the Portfolio, Activity & settings and
// onboarding sources, read as text.
const guideFiles = import.meta.glob('../../../docs/product/USER-GUIDE.md', {
  eager: true,
  query: '?raw',
  import: 'default',
}) as Record<string, string>
const guide = guideFiles['../../../docs/product/USER-GUIDE.md']

const sources = {
  ...import.meta.glob(
    [
      '../components/PortfolioPage.tsx',
      '../components/ActivityPage.tsx',
      '../components/SettingsPanel.tsx',
      '../components/OnboardingPage.tsx',
      '../components/accountPages.css',
      './portfolioApi.ts',
      './activityApi.ts',
      './portfolioSample.ts',
    ],
    { eager: true, query: '?raw', import: 'default' },
  ),
} as Record<string, string>

describe('USER-GUIDE.md', () => {
  it('exists', () => {
    expect(typeof guide).toBe('string')
  })

  const sections = [
    'Approve a trade from your phone',
    'Pause new entries or stop everything',
    'Sunday 2FA',
    "What 'unavailable' means",
    'Paper acceptance progress',
  ]
  it.each(sections)('has the section %s', (title) => {
    expect(guide).toMatch(new RegExp(`^## ${title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`, 'm'))
  })

  it('names every control by its on-screen label', () => {
    for (const label of Object.values(CONTROL_LABELS)) expect(guide).toContain(label)
  })

  it('states the acceptance thresholds the contract fixes', () => {
    expect(guide).toContain(`${ACCEPTANCE_THRESHOLDS.streak_sessions} consecutive`)
    expect(guide).toContain(`${ACCEPTANCE_THRESHOLDS.heartbeat_min_percent}%`)
    expect(guide).toContain(`${ACCEPTANCE_THRESHOLDS.lifecycles_per_kind} completed`)
    expect(guide).toContain(`${ACCEPTANCE_THRESHOLDS.max_exit_unguarded_s} seconds`)
    for (const id of DRILL_IDS) expect(guide).toContain(DRILL_LABELS[id])
  })

  it('does not promise a Family key fact that Today does not carry', () => {
    const start = guide.slice(guide.indexOf('## Getting started'), guide.indexOf('## Approve a trade'))
    const step4 = start.slice(start.indexOf('4. **Family command key installed**'), start.indexOf('5. **'))
    expect(step4.length).toBeGreaterThan(0)
    expect(step4).toMatch(/Unknown/)
    expect(step4).not.toMatch(/Today\s+raises/)
  })

  it('never suggests that acceptance or anything else turns on live trading', () => {
    // markdown wraps lines, so words are matched across any whitespace
    expect(guide).toMatch(/never\s+unlocks\s+live\s+trading/)
    expect(guide).not.toMatch(/live\s+trading\s+(is|will\s+be)\s+(enabled|unlocked|on)\b/i)
  })
})

describe('text hygiene of the lane sources', () => {
  it('reads every lane source', () => {
    expect(Object.keys(sources)).toHaveLength(8)
  })

  it.each([...Object.entries(sources), ['USER-GUIDE.md', guide]])('%s has no en or em dash', (_name, text) => {
    expect(text).not.toContain(String.fromCharCode(0x2013))
    expect(text).not.toContain(String.fromCharCode(0x2014))
  })

  it.each(Object.entries(sources))('%s fetches nothing from an absolute path', (_name, text) => {
    expect(text).not.toMatch(/fetch(Json)?\(\s*['"`]\//)
    expect(text).not.toMatch(/['"`]\/api\//)
  })
})
