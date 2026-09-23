import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PollState } from '../hooks/usePoll'
import { AppShell } from './AppShell'

const poll = (over: Partial<PollState<unknown>>): PollState<unknown> => ({
  data: null,
  error: null,
  lastSuccessAt: null,
  isStale: false,
  refresh: () => undefined,
  ...over,
})

// M8 flash review: "● live" sat beside hours-old marks and "delayed ·
// CBOE" chips. It only ever meant "the cockpit API answered recently".
describe('AppShell connection badge', () => {
  it('names what it measures: the API, not data freshness', () => {
    render(<AppShell title="Plans" poll={poll({ data: {}, lastSuccessAt: 1 })}>x</AppShell>)
    expect(screen.getByText('● API connected')).toBeTruthy()
    expect(screen.queryByText(/live/)).toBeNull()
  })

  it('says loading before the first response instead of claiming a connection', () => {
    render(<AppShell title="Plans" poll={poll({})}>x</AppShell>)
    expect(screen.getByText('○ loading')).toBeTruthy()
  })
})

describe('AppShell nav (portal invariant)', () => {
  it('nav links are hash-only and document-relative', () => {
    render(<AppShell title="Plans">x</AppShell>)
    const links = screen.getAllByRole('link')
    for (const a of links) {
      expect(a.getAttribute('href')?.startsWith('/')).toBe(false)
    }
    expect(screen.getByRole('link', { name: 'Discover' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Plans' }).getAttribute('aria-current')).toBe(
      'page',
    )
  })
})
