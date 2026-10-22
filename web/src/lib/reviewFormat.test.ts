import { describe, expect, it } from 'vitest'
import { MANUAL_MAX_HOLD_SESSIONS, WAVE3_REASONS } from './contracts'
import { CLIENT_REASONS, REASON_TEXT, detailWords, formatCents, formatCountdown, reasonText } from './reviewFormat'
import { todayUnavailableFixture, vocabularyFixture } from './reviewTestKit'

// The Python sources that spell the server's reason codes: the Wave 2
// `Blocker` enum, the loopback authority's refusal details, the command
// store's unavailable reasons, the desk owner's plain refusal strings and
// the manual ticket's detail codes (desk_entries.py).
const sources = import.meta.glob(
  [
    '../../../src/tree_options/trex/contracts/vocabulary.py',
    '../../../src/tree_options/trex_web/commands_view.py',
    '../../../src/tree_options/trex_web/command_store.py',
    '../../../src/tree_options/trex/desk_commands.py',
    '../../../src/tree_options/trex/desk_entries.py',
  ],
  { eager: true, query: '?raw', import: 'default' },
) as Record<string, string>

function source(name: string): string {
  const hit = Object.entries(sources).find(([path]) => path.endsWith(`/${name}`))
  if (!hit) throw new Error(`source ${name} not found`)
  return hit[1]
}

function serverVocabulary(): Set<string> {
  const vocab = new Set<string>()
  const blockers = source('vocabulary.py').split('class DeskEvent')[0]
  for (const m of blockers.matchAll(/^\s+[A-Z0-9_]+ = "([^"]+)"$/gm)) vocab.add(m[1])
  const view = source('commands_view.py')
  const details = view.slice(view.indexOf('Refusal details:'), view.indexOf('"""', view.indexOf('Refusal details:')))
  for (const m of details.matchAll(/``([a-z_]+)``/g)) vocab.add(m[1])
  for (const m of view.matchAll(/"detail": "([a-z_]+)"/g)) vocab.add(m[1])
  for (const m of source('command_store.py').matchAll(/CommandStoreUnavailable\(\s*"([a-z_]+)"/g)) vocab.add(m[1])
  for (const m of source('desk_commands.py').matchAll(/^GATEWAY_UNAVAILABLE = "([a-z_]+)"$/gm)) vocab.add(m[1])
  for (const m of source('desk_entries.py').matchAll(/^DEADLINE_BEYOND_MAX_HOLD = "([a-z_]+)"$/gm)) vocab.add(m[1])
  for (const reason of WAVE3_REASONS) vocab.add(reason)
  for (const refusal of vocabularyFixture().family_refusals as string[]) vocab.add(refusal)
  return vocab
}

describe('money and countdowns', () => {
  it('formats integer cents exactly', () => {
    expect(formatCents(26860)).toBe('$268.60')
    expect(formatCents(134300)).toBe('$1,343.00')
    expect(formatCents(2500000)).toBe('$25,000.00')
    expect(formatCents(5)).toBe('$0.05')
    expect(formatCents(0)).toBe('$0.00')
    expect(formatCents(-4700)).toBe('-$47.00')
    expect(() => formatCents(1.5)).toThrow(RangeError)
  })

  it('counts down as m:ss, h:mm:ss past an hour, never below zero', () => {
    expect(formatCountdown(480)).toBe('8:00')
    expect(formatCountdown(59)).toBe('0:59')
    expect(formatCountdown(3725)).toBe('1:02:05')
    expect(formatCountdown(-3)).toBe('0:00')
  })
})

describe('reason words come from the vocabulary', () => {
  const vocab = serverVocabulary()

  it('the parsed server vocabulary is the real one (sanity)', () => {
    for (const code of ['approval_epoch_mismatch', 'command_expired', 'origin_mismatch', 'commands_dir_unwritable', 'gateway_unavailable', 'assertion_sub_not_owner', 'holdings_missing']) {
      expect(vocab.has(code), code).toBe(true)
    }
    expect(vocab.has('entry_approved')).toBe(false) // a DeskEvent, not a Blocker
  })

  it('every worded code is a server code or a named client-side code', () => {
    for (const code of CLIENT_REASONS) expect(vocab.has(code), code).toBe(false)
    const client = new Set<string>(CLIENT_REASONS)
    const invented = Object.keys(REASON_TEXT).filter((code) => !vocab.has(code) && !client.has(code))
    expect(invented).toEqual([])
    for (const code of CLIENT_REASONS) expect(REASON_TEXT[code], code).toBeTruthy()
  })

  it('every Wave 3 reason and family refusal has words', () => {
    for (const code of [...WAVE3_REASONS, ...(vocabularyFixture().family_refusals as string[])]) {
      expect(REASON_TEXT[code], code).toBeTruthy()
    }
  })

  it('every reason the unavailable Today fixture carries has words', () => {
    const reasons: string[] = []
    const walk = (node: unknown): void => {
      if (Array.isArray(node)) node.forEach(walk)
      else if (node && typeof node === 'object') {
        for (const [key, value] of Object.entries(node)) {
          if (key === 'unavailable_reason' && typeof value === 'string') reasons.push(value)
          else walk(value)
        }
      }
    }
    walk(todayUnavailableFixture())
    expect(reasons.length).toBeGreaterThan(8)
    for (const code of reasons) expect(REASON_TEXT[code], code).toBeTruthy()
  })

  it('an unknown code still reads as words and keeps the code visible', () => {
    expect(reasonText('approval_epoch_mismatch')).toBe(REASON_TEXT.approval_epoch_mismatch)
    expect(reasonText('some_new_code')).toBe('some new code')
  })

  it('every Wave 5 refusal, view reason and manual ticket detail has plain words', () => {
    const wave5 = ['manual_ticket_invalid', 'drafts_dir_unreadable', 'draft_unreadable', 'deadline_beyond_max_hold']
    for (const code of wave5) {
      expect(vocab.has(code), code).toBe(true)
      expect(REASON_TEXT[code], code).toBeTruthy()
      expect(reasonText(code), code).not.toBe(code.replace(/_/g, ' '))
    }
    expect(REASON_TEXT.deadline_beyond_max_hold).toContain(`${MANUAL_MAX_HOLD_SESSIONS} market sessions`)
  })

  it('a detail that is a known code reads as words; free text stays as sent', () => {
    expect(detailWords('deadline_beyond_max_hold')).toBe(REASON_TEXT.deadline_beyond_max_hold)
    expect(detailWords('the stop is not below the bid')).toBeNull()
    expect(detailWords('order_unbuildable:RuntimeError')).toBeNull()
    expect(detailWords('')).toBeNull()
  })
})
