import { act, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
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

// Hand-written oracle (C3.3): desktop labels, phone tab labels, hashes.
const DESKTOP = ['Today', 'Experiments', 'Trade review', 'Portfolio', 'Activity & settings']
const PHONE = ['Today', 'Experiments', 'Review', 'Portfolio', 'Activity']
const HASHES = ['#/today', '#/experiments', '#/trade-review', '#/portfolio', '#/activity']
// The pre-Wave-3 views under each destination's Advanced menu (default 16).
const ADVANCED: Record<string, string[]> = {
  '#/today': [],
  '#/experiments': ['Discover', 'Research Lab', 'Action model', 'Quant lab', 'Research and paper', 'Trade floor'],
  '#/trade-review': ['Market'],
  '#/portfolio': ['Plans', 'Performance'],
  '#/activity': [],
}

const realMatchMedia = window.matchMedia
let mediaListeners: Array<() => void> = []
let phoneWidth = false

function viewport(phone: boolean) {
  phoneWidth = phone
  window.matchMedia = ((query: string) => ({
    // a phone answers every max-width query; a desktop none
    get matches() {
      return phoneWidth && query.includes('max-width')
    },
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: (_type: string, listener: () => void) => mediaListeners.push(listener),
    removeEventListener: (_type: string, listener: () => void) => {
      mediaListeners = mediaListeners.filter((l) => l !== listener)
    },
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

function at(hash: string) {
  history.replaceState(null, '', hash)
}

function linkNames(nav: HTMLElement): string[] {
  return within(nav)
    .getAllByRole('link')
    .map((a) => a.textContent ?? '')
}

beforeEach(() => {
  // the control bar polls api/today; no backend here
  vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 404 })))
  viewport(false)
  at('#/today')
})

afterEach(() => {
  window.matchMedia = realMatchMedia
  mediaListeners = []
  vi.unstubAllGlobals()
  at('#')
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

describe('AppShell navigation at desktop width', () => {
  it('shows the five destinations in order, hash-only, and no phone tab bar', () => {
    render(<AppShell title="Today">x</AppShell>)
    const nav = screen.getByRole('navigation', { name: 'Destinations' })
    expect(linkNames(nav)).toEqual(DESKTOP)
    expect(within(nav).getAllByRole('link').map((a) => a.getAttribute('href'))).toEqual(HASHES)
    expect(screen.queryByRole('navigation', { name: 'Tabs' })).toBeNull()
  })

  it.each(HASHES.map((hash, i) => [hash, DESKTOP[i]]))('%s marks %s as the current page', (hash, label) => {
    at(hash)
    render(<AppShell title={label}>x</AppShell>)
    const nav = screen.getByRole('navigation', { name: 'Destinations' })
    const current = within(nav)
      .getAllByRole('link')
      .filter((a) => a.hasAttribute('aria-current'))
    expect(current.map((a) => [a.textContent, a.getAttribute('aria-current')])).toEqual([[label, 'page']])
  })

  it('the root hash is Today', () => {
    at('#/')
    render(<AppShell title="Today">x</AppShell>)
    const nav = screen.getByRole('navigation', { name: 'Destinations' })
    expect(within(nav).getByRole('link', { name: 'Today' }).getAttribute('aria-current')).toBe('page')
  })

  it('an advanced view marks its destination as the current section and itself as the page', () => {
    at('#/plans')
    render(<AppShell title="Plans">x</AppShell>)
    const nav = screen.getByRole('navigation', { name: 'Destinations' })
    expect(within(nav).getByRole('link', { name: 'Portfolio' }).getAttribute('aria-current')).toBe('true')
    const advanced = screen.getByRole('navigation', { name: 'Advanced views' })
    expect(within(advanced).getByRole('link', { name: 'Plans' }).getAttribute('aria-current')).toBe('page')
    expect(advanced.querySelector('details')?.open).toBe(true)
  })

  it('a detail page under a destination keeps that destination current', () => {
    at(`#/trade-review/${'a'.repeat(64)}`)
    render(<AppShell title="Proposal">x</AppShell>)
    const nav = screen.getByRole('navigation', { name: 'Destinations' })
    expect(within(nav).getByRole('link', { name: 'Trade review' }).getAttribute('aria-current')).toBe('true')
  })
})

describe('AppShell navigation at phone width', () => {
  it('shows the bottom tab bar with the short labels and no desktop nav', () => {
    viewport(true)
    at('#/trade-review')
    render(<AppShell title="Trade review">x</AppShell>)
    const tabs = screen.getByRole('navigation', { name: 'Tabs' })
    expect(linkNames(tabs)).toEqual(PHONE)
    expect(within(tabs).getAllByRole('link').map((a) => a.getAttribute('href'))).toEqual(HASHES)
    expect(within(tabs).getByRole('link', { name: 'Review' }).getAttribute('aria-current')).toBe('page')
    expect(screen.queryByRole('navigation', { name: 'Destinations' })).toBeNull()
    // the old views stay one tap away on a phone too
    expect(linkNames(screen.getByRole('navigation', { name: 'Advanced views' }))).toEqual(['Market'])
  })

  it('switches between the layouts when the viewport crosses the breakpoint', () => {
    render(<AppShell title="Today">x</AppShell>)
    expect(screen.getByRole('navigation', { name: 'Destinations' })).toBeTruthy()
    act(() => {
      phoneWidth = true
      for (const listener of mediaListeners) listener()
    })
    expect(screen.getByRole('navigation', { name: 'Tabs' })).toBeTruthy()
    expect(screen.queryByRole('navigation', { name: 'Destinations' })).toBeNull()
  })
})

describe('AppShell advanced menus', () => {
  it.each(Object.entries(ADVANCED))('%s lists %j', (hash, labels) => {
    at(hash)
    render(<AppShell title="x">x</AppShell>)
    const advanced = screen.queryByRole('navigation', { name: 'Advanced views' })
    if (labels.length === 0) {
      expect(advanced).toBeNull()
      return
    }
    expect(advanced).not.toBeNull()
    expect(linkNames(advanced as HTMLElement)).toEqual(labels)
    expect(advanced?.querySelector('details')?.open).toBe(false)
  })
})

describe('AppShell chrome', () => {
  it('every link is hash-only and document-relative (portal invariant)', () => {
    at('#/experiments')
    render(<AppShell title="Experiments">x</AppShell>)
    const links = screen.getAllByRole('link')
    expect(links.length).toBeGreaterThan(5)
    for (const a of links) expect(a.getAttribute('href')?.startsWith('#/'), a.textContent ?? '').toBe(true)
  })

  it('an unknown hash says it fell back to Today', () => {
    at('#/no-such-page')
    render(<AppShell title="Today">x</AppShell>)
    const notice = screen.getByText(/showing Today/)
    expect(notice.textContent).toContain('#/no-such-page')
  })

  it('says paper only and carries the desk control bar on every page', () => {
    at('#/stats')
    render(<AppShell title="Performance">x</AppShell>)
    expect(screen.getByText(/paper only/i)).toBeTruthy()
    const bar = screen.getByRole('region', { name: 'Desk controls' })
    expect(within(bar).getByRole('button', { name: 'Pause new entries' })).toBeTruthy()
    expect(within(bar).getByRole('button', { name: 'Emergency stop (also stops exits)' })).toBeTruthy()
  })
})
