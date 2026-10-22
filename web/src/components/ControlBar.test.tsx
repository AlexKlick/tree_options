import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Controls, KillSwitchState, TodayView } from '../lib/contracts'
import { ControlBar, ControlBarView, CommandOutbox } from './ControlBar'

// The shared Wave 3 fixtures (tests/contracts/fixtures/wave3/), the same
// files Python round-trips through its models.
const wave3 = import.meta.glob('../../../tests/contracts/fixtures/wave3/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown>

function fixture<T>(name: string): T {
  const hit = Object.entries(wave3).find(([path]) => path.endsWith(`/${name}`))
  if (!hit) throw new Error(`fixture ${name} not found`)
  return structuredClone(hit[1]) as T
}

// Hand-written oracle (not CONTROL_LABELS): the owner-facing words.
const LABEL = {
  pause_entries: 'Pause new entries',
  resume_entries: 'Resume new entries',
  halt: 'Emergency stop (also stops exits)',
  flatten: 'Close all desk positions',
  resume_all: 'Clear emergency stop, close-all and pause',
} as const

const KEY_A = '5f0c2a8e-1b3d-4c6e-9a7b-2d4e6f8a0b1c'
const KEY_B = '0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'
const AT = new Date('2026-10-22T14:00:00.700Z')

function bodyText(kind: string, key: string): string {
  // exact bytes, key order of trex-command/1 (buildCommandBody), whole seconds
  return (
    `{"schema":"trex-command/1","idempotency_key":"${key}","kind":"${kind}",` +
    `"args":{"reason":"from the control bar"},` +
    `"issued_at":"2026-10-22T14:00:00Z","expires_at":"2026-10-22T14:05:00Z"}`
  )
}

function resultDoc(kind: string, key: string, status: string, extra: Record<string, unknown> = {}) {
  return {
    schema: 'trex-command-result/1',
    idempotency_key: key,
    kind,
    command_body_sha256: 'a'.repeat(64),
    status,
    reason: null,
    detail: '',
    result: null,
    at: '2026-10-22T14:00:01Z',
    ...extra,
  }
}

function json(status: number, doc: unknown): Response {
  return new Response(JSON.stringify(doc), { status, headers: { 'content-type': 'application/json' } })
}

type FetchArgs = [input: RequestInfo | URL, init?: RequestInit]

function keys(...list: string[]): () => string {
  const queue = [...list]
  return () => {
    const next = queue.shift()
    if (!next) throw new Error('no more keys')
    return next
  }
}

function posts(fetchMock: { mock: { calls: FetchArgs[] } }): { url: string; init: RequestInit }[] {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method === 'POST')
    .map(([url, init]) => ({ url: String(url), init: init as RequestInit }))
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('CommandOutbox: command bodies', () => {
  it.each(Object.keys(LABEL))('%s posts one trex-command/1 to the relative api/commands', async (kind) => {
    const fetchMock = vi.fn(async (..._args: FetchArgs) => json(202, resultDoc(kind, KEY_A, 'accepted')))
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A) })
    const sent = await outbox.send(kind as keyof typeof LABEL, AT)
    expect(sent.state).toBe('answered')
    const [post] = posts(fetchMock)
    expect(post.url).toBe('api/commands')
    expect(post.init.body).toBe(bodyText(kind, KEY_A))
    expect(JSON.parse(post.init.body as string)).not.toHaveProperty('issued_by')
    expect(new Headers(post.init.headers).get('content-type')).toBe('application/json')
    expect(post.init.credentials).toBe('same-origin')
    expect(outbox.pending()).toBeNull()
  })

  it('mints a lower-case UUID by default', async () => {
    const fetchMock = vi.fn(async (..._args: FetchArgs) => json(202, resultDoc('pause_entries', KEY_A, 'accepted')))
    await new CommandOutbox({ fetch: fetchMock }).send('pause_entries', AT)
    const key = JSON.parse(posts(fetchMock)[0].init.body as string).idempotency_key as string
    expect(key).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  })
})

describe('CommandOutbox: retries reuse the idempotency key', () => {
  it('a network failure keeps the exact body; the retry resends it, then a new command gets a new key', async () => {
    const answers: Array<() => Promise<Response>> = [
      async () => {
        throw new TypeError('Failed to fetch')
      },
      async () => json(202, resultDoc('halt', KEY_A, 'accepted')),
      async () => json(202, resultDoc('halt', KEY_B, 'accepted')),
    ]
    const fetchMock = vi.fn((..._args: FetchArgs) => answers.shift()!())
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A, KEY_B) })

    const first = await outbox.send('halt', AT)
    expect(first.state).toBe('unconfirmed')
    expect(outbox.pending()).toEqual({ kind: 'halt', key: KEY_A, body: bodyText('halt', KEY_A) })

    // later clock, same body: same key, same issued_at
    const retry = await outbox.send('halt', new Date('2026-10-22T14:02:00Z'))
    expect(retry.state).toBe('answered')
    expect(posts(fetchMock).map((p) => p.init.body)).toEqual([bodyText('halt', KEY_A), bodyText('halt', KEY_A)])
    expect(outbox.pending()).toBeNull()

    await outbox.send('halt', AT)
    expect(posts(fetchMock)[2].init.body).toBe(bodyText('halt', KEY_B))
  })

  it('a kept body past its expiry is never resent: the next press mints a fresh command (review P2)', async () => {
    const answers: Array<() => Promise<Response>> = [
      async () => {
        throw new TypeError('Failed to fetch')
      },
      async () => json(202, resultDoc('halt', KEY_B, 'accepted')),
    ]
    const fetchMock = vi.fn((..._args: FetchArgs) => answers.shift()!())
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A, KEY_B) })
    expect((await outbox.send('halt', AT)).state).toBe('unconfirmed') // expires 14:05:00Z
    // ten minutes later, in an emergency: the 14:00 body would only be refused
    // command_expired, costing the owner one more confirm cycle
    const later = new Date('2026-10-22T14:10:00Z')
    expect((await outbox.send('halt', later)).state).toBe('answered')
    const [, second] = posts(fetchMock)
    const body = JSON.parse(second.init.body as string)
    expect(body.idempotency_key).toBe(KEY_B)
    expect((body.issued_at as string) > '2026-10-22T14:05:00Z').toBe(true)
  })

  it('a kept body about to expire (under 5 s left) is not resent either', async () => {
    const answers: Array<() => Promise<Response>> = [
      async () => {
        throw new TypeError('Failed to fetch')
      },
      async () => json(202, resultDoc('pause_entries', KEY_B, 'accepted')),
    ]
    const fetchMock = vi.fn((..._args: FetchArgs) => answers.shift()!())
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A, KEY_B) })
    await outbox.send('pause_entries', AT)
    await outbox.send('pause_entries', new Date('2026-10-22T14:04:56Z'))
    expect(JSON.parse(posts(fetchMock)[1].init.body as string).idempotency_key).toBe(KEY_B)
  })

  it.each([
    ['a 503', () => json(503, { error: 'command_store_unavailable', reason: 'commands_dir_unwritable' })],
    ['a 502 HTML page', () => new Response('<html>bad gateway</html>', { status: 502, headers: { 'content-type': 'text/html' } })],
    ['an HTML sign-in page', () => new Response('<html>sign in</html>', { status: 403, headers: { 'content-type': 'text/html' } })],
    ['a 202 that is not a command result', () => json(202, { hello: 'world' })],
  ])('%s is unconfirmed and keeps the key', async (_name, answer) => {
    const fetchMock = vi.fn(async (..._args: FetchArgs) => answer())
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A, KEY_B) })
    expect((await outbox.send('pause_entries', AT)).state).toBe('unconfirmed')
    await outbox.send('pause_entries', AT)
    expect(posts(fetchMock).map((p) => p.init.body)).toEqual([
      bodyText('pause_entries', KEY_A),
      bodyText('pause_entries', KEY_A),
    ])
  })

  it.each([
    ['duplicate', 200, resultDoc('flatten', KEY_A, 'duplicate', { reason: 'duplicate_command', result: { first_status: 'applied' } })],
    ['refused', 409, resultDoc('flatten', KEY_A, 'refused', { reason: 'command_expired', detail: 'expired' })],
  ])('a %s answer is final: the next command gets a new key', async (_name, status, doc) => {
    const fetchMock = vi.fn(async (..._args: FetchArgs) => json(status, doc))
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A, KEY_B) })
    const sent = await outbox.send('flatten', AT)
    expect(sent).toMatchObject({ state: 'answered', httpStatus: status })
    expect(outbox.pending()).toBeNull()
    await outbox.send('flatten', AT)
    expect(posts(fetchMock)[1].init.body).toBe(bodyText('flatten', KEY_B))
  })

  it('an authority refusal (403 JSON) is final and nothing is kept', async () => {
    const fetchMock = vi.fn(async (..._args: FetchArgs) =>
      json(403, { error: 'command_unauthorized', reason: 'command_unauthorized', detail: 'assertion_missing' }),
    )
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A) })
    expect(await outbox.send('halt', AT)).toEqual({
      state: 'refused',
      httpStatus: 403,
      error: 'command_unauthorized',
      detail: 'assertion_missing',
    })
    expect(outbox.pending()).toBeNull()
  })

  it('another command kind drops the unconfirmed one (never replays an old pause after a resume)', async () => {
    const answers: Array<() => Promise<Response>> = [
      async () => {
        throw new TypeError('offline')
      },
      async () => json(202, resultDoc('resume_entries', KEY_B, 'accepted')),
      async () => json(202, resultDoc('pause_entries', 'c'.repeat(8), 'accepted')),
    ]
    const fetchMock = vi.fn((..._args: FetchArgs) => answers.shift()!())
    const third = 'f0e1d2c3-b4a5-4968-8776-655443322110'
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A, KEY_B, third) })
    await outbox.send('pause_entries', AT)
    await outbox.send('resume_entries', AT)
    await outbox.send('pause_entries', AT)
    expect(posts(fetchMock).map((p) => JSON.parse(p.init.body as string).idempotency_key)).toEqual([KEY_A, KEY_B, third])
  })

  it('a request that never answers times out as unconfirmed', async () => {
    const fetchMock = vi.fn(
      (_url: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
        }),
    )
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A), timeoutMs: 20 })
    const sent = await outbox.send('pause_entries', AT)
    expect(sent.state).toBe('unconfirmed')
    expect(outbox.pending()?.key).toBe(KEY_A)
  })
})

function clearControls(): Controls {
  return fixture<TodayView>('today.json').controls
}

function standing(controls: Controls, which: 'pause_entries' | 'halt' | 'flatten'): Controls {
  const name = ({ pause_entries: 'PAUSE_ENTRIES', halt: 'HALT', flatten: 'FLATTEN' } as const)[which]
  const state: KillSwitchState = {
    name,
    status: 'standing',
    unavailable_reason: null,
    since: '2026-10-22T13:45:00Z',
    reason: 'owner',
  }
  return { ...controls, [which]: state } as Controls
}

function okOutbox() {
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse((init?.body as string) ?? '{}')
    return json(202, resultDoc(body.kind, body.idempotency_key, 'accepted'))
  })
  return { fetchMock, outbox: new CommandOutbox({ fetch: fetchMock }) }
}

function buttonNames(): string[] {
  return screen.getAllByRole('button').map((b) => b.textContent ?? '')
}

describe('ControlBarView: what it offers', () => {
  it('everything clear: Pause is the primary control; emergency controls are offered, nothing to clear', () => {
    render(<ControlBarView controls={clearControls()} unavailableReason={null} outbox={okOutbox().outbox} />)
    const primary = screen.getByRole('button', { name: LABEL.pause_entries })
    expect(primary.className).toContain('control-primary')
    expect(buttonNames()).toEqual([LABEL.pause_entries, LABEL.halt, LABEL.flatten])
    expect(screen.getByText(/New entries: allowed/)).toBeTruthy()
    // the pause's rule as policy, never a claim about the exits' state now
    expect(screen.getByText(/Pausing stops new entries only; it never stops exits/)).toBeTruthy()
    expect(screen.queryByText(/exits keep running/i)).toBeNull()
    // the fixture's queued pause, waiting for the desk
    expect(screen.getByText(/Waiting for the desk:/).textContent).toContain(LABEL.pause_entries)
  })

  it('paused: Resume replaces Pause', () => {
    render(
      <ControlBarView controls={standing(clearControls(), 'pause_entries')} unavailableReason={null} outbox={okOutbox().outbox} />,
    )
    expect(buttonNames()).toEqual([LABEL.resume_entries, LABEL.halt, LABEL.flatten])
    expect(screen.getByText(/New entries: paused since/)).toBeTruthy()
  })

  it('emergency stop standing: offers to clear it and says exits are stopped', () => {
    render(<ControlBarView controls={standing(clearControls(), 'halt')} unavailableReason={null} outbox={okOutbox().outbox} />)
    expect(buttonNames()).toEqual([LABEL.pause_entries, LABEL.flatten, LABEL.resume_all])
    expect(screen.getByText(/Emergency stop: ON since/).textContent).toMatch(/exits are stopped/)
    // review P2: never "allowed" while the emergency stop blocks every order
    expect(screen.queryByText(/New entries: allowed/)).toBeNull()
    expect(screen.getByText(/New entries: blocked/).textContent).toContain('emergency stop')
  })

  it('close-all standing: offers to clear it and says entries are blocked', () => {
    render(<ControlBarView controls={standing(clearControls(), 'flatten')} unavailableReason={null} outbox={okOutbox().outbox} />)
    expect(buttonNames()).toEqual([LABEL.pause_entries, LABEL.halt, LABEL.resume_all])
    expect(screen.getByText(/New entries: blocked/).textContent).toContain('close-all')
  })

  it('unknown state renders as unavailable with the reason and offers every control', () => {
    const down = fixture<TodayView>('today_unavailable.json').controls
    render(<ControlBarView controls={down} unavailableReason={null} outbox={okOutbox().outbox} />)
    expect(buttonNames()).toEqual([LABEL.pause_entries, LABEL.resume_entries, LABEL.halt, LABEL.flatten, LABEL.resume_all])
    expect(screen.getByText(/New entries: unavailable/).textContent).toContain('kill_file_unreadable')
    expect(screen.getByText(/Queued commands: unavailable/).textContent).toContain('command_store_unavailable')
    expect(screen.queryByText(/allowed/)).toBeNull()
  })

  it('compact (phone): Pause stays in view, the stops sit behind "More controls" (review P2)', () => {
    const { container } = render(
      <ControlBarView controls={clearControls()} unavailableReason={null} outbox={okOutbox().outbox} compact />,
    )
    const more = container.querySelector('details.control-more') as HTMLDetailsElement
    expect(more).not.toBeNull()
    expect(more.open).toBe(false)
    expect(within(more).getByText('More controls')).toBeTruthy()
    const tucked = Array.from(more.querySelectorAll('button')).map((b) => b.textContent)
    expect(tucked).toEqual([LABEL.halt, LABEL.flatten])
    const inView = Array.from(container.querySelectorAll('button'))
      .filter((b) => !more.contains(b))
      .map((b) => b.textContent)
    expect(inView).toEqual([LABEL.pause_entries])
  })

  it('compact with the emergency stop standing keeps its clear control in view', () => {
    const { container } = render(
      <ControlBarView controls={standing(clearControls(), 'halt')} unavailableReason={null} outbox={okOutbox().outbox} compact />,
    )
    const more = container.querySelector('details.control-more') as HTMLDetailsElement
    const inView = Array.from(container.querySelectorAll('button'))
      .filter((b) => !more.contains(b))
      .map((b) => b.textContent)
    expect(inView).toEqual([LABEL.pause_entries, LABEL.resume_all])
    expect(Array.from(more.querySelectorAll('button')).map((b) => b.textContent)).toEqual([LABEL.flatten])
  })

  it('compact: a tucked stop still asks for confirmation before sending', async () => {
    const { fetchMock, outbox } = okOutbox()
    const { container } = render(
      <ControlBarView controls={clearControls()} unavailableReason={null} outbox={outbox} compact />,
    )
    const more = container.querySelector('details.control-more') as HTMLDetailsElement
    fireEvent.click(within(more).getByRole('button', { name: LABEL.halt }))
    expect(posts(fetchMock)).toEqual([])
    fireEvent.click(screen.getByRole('button', { name: `Yes: ${LABEL.halt}` }))
    await waitFor(() => expect(posts(fetchMock)).toHaveLength(1))
  })

  it('no Today view at all: unavailable with the reason, every control offered', () => {
    render(<ControlBarView controls={null} unavailableReason="today view unreachable (HTTP 404)" outbox={okOutbox().outbox} />)
    expect(buttonNames()).toEqual([LABEL.pause_entries, LABEL.resume_entries, LABEL.halt, LABEL.flatten, LABEL.resume_all])
    expect(screen.getByText(/New entries: unavailable/).textContent).toContain('today view unreachable (HTTP 404)')
  })
})

describe('ControlBarView: sending', () => {
  it('Pause sends at once (no confirm) and reports the queued command', async () => {
    const { fetchMock, outbox } = okOutbox()
    const onSent = vi.fn()
    render(<ControlBarView controls={clearControls()} unavailableReason={null} outbox={outbox} onSent={onSent} />)
    fireEvent.click(screen.getByRole('button', { name: LABEL.pause_entries }))
    await screen.findByText(/Sent: Pause new entries/)
    const [post] = posts(fetchMock)
    expect(JSON.parse(post.init.body as string)).toMatchObject({ kind: 'pause_entries', args: { reason: 'from the control bar' } })
    expect(onSent).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['halt', LABEL.halt, /stops new entries AND automatic exits/],
    ['flatten', LABEL.flatten, /closes every position the desk owns/],
    ['resume_all', LABEL.resume_all, /removes the emergency stop, close-all and the entry pause/],
  ] as const)('%s asks for confirmation first; cancel sends nothing', async (kind, label, explains) => {
    const { fetchMock, outbox } = okOutbox()
    const controls = kind === 'resume_all' ? standing(clearControls(), 'halt') : clearControls()
    render(<ControlBarView controls={controls} unavailableReason={null} outbox={outbox} />)

    fireEvent.click(screen.getByRole('button', { name: label }))
    const confirm = screen.getByRole('group', { name: `Confirm: ${label}` })
    expect(confirm.textContent).toMatch(explains)
    expect(fetchMock).not.toHaveBeenCalled()
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('group', { name: `Confirm: ${label}` })).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: label }))
    fireEvent.click(
      within(screen.getByRole('group', { name: `Confirm: ${label}` })).getByRole('button', { name: `Yes: ${label}` }),
    )
    await screen.findByText(new RegExp(`Sent: ${label.replace(/[()]/g, '\\$&')}`))
    expect(posts(fetchMock).map((p) => JSON.parse(p.init.body as string).kind)).toEqual([kind])
  })

  it('Retry after a network failure resends the same body with the same key', async () => {
    const answers: Array<() => Promise<Response>> = [
      async () => {
        throw new TypeError('Failed to fetch')
      },
      async () => json(200, resultDoc('pause_entries', KEY_A, 'duplicate', { reason: 'duplicate_command', result: { first_status: 'accepted' } })),
    ]
    const fetchMock = vi.fn((..._args: FetchArgs) => answers.shift()!())
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A, KEY_B) })
    render(<ControlBarView controls={clearControls()} unavailableReason={null} outbox={outbox} now={() => AT} />)
    fireEvent.click(screen.getByRole('button', { name: LABEL.pause_entries }))
    const status = await screen.findByText(/Not confirmed/)
    expect(status.textContent).toContain(KEY_A.slice(0, 8))
    fireEvent.click(screen.getByRole('button', { name: 'Retry: Pause new entries' }))
    await screen.findByText(/Already received: Pause new entries/)
    expect(screen.getByText(/first outcome: accepted/)).toBeTruthy()
    expect(posts(fetchMock).map((p) => p.init.body)).toEqual([bodyText('pause_entries', KEY_A), bodyText('pause_entries', KEY_A)])
    expect(screen.queryByRole('button', { name: /Retry/ })).toBeNull()
  })

  it('a refusal shows its reason and offers no retry', async () => {
    const fetchMock = vi.fn(async (..._args: FetchArgs) =>
      json(409, resultDoc('pause_entries', KEY_A, 'refused', { reason: 'command_expired', detail: 'expired at 2026-10-22T14:05:00Z' })),
    )
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A) })
    render(<ControlBarView controls={clearControls()} unavailableReason={null} outbox={outbox} />)
    fireEvent.click(screen.getByRole('button', { name: LABEL.pause_entries }))
    expect((await screen.findByText(/Refused: Pause new entries/)).textContent).toContain('command_expired')
    expect(screen.queryByRole('button', { name: /Retry/ })).toBeNull()
  })

  it('buttons are disabled while a command is in flight', async () => {
    let release: (r: Response) => void = () => undefined
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => (release = resolve)))
    const outbox = new CommandOutbox({ fetch: fetchMock, newKey: keys(KEY_A) })
    render(<ControlBarView controls={clearControls()} unavailableReason={null} outbox={outbox} />)
    fireEvent.click(screen.getByRole('button', { name: LABEL.pause_entries }))
    await waitFor(() => expect((screen.getByRole('button', { name: LABEL.halt }) as HTMLButtonElement).disabled).toBe(true))
    fireEvent.click(screen.getByRole('button', { name: LABEL.pause_entries }))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await act(async () => release(json(202, resultDoc('pause_entries', KEY_A, 'accepted'))))
    await screen.findByText(/Sent: Pause new entries/)
    expect((screen.getByRole('button', { name: LABEL.halt }) as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('ControlBar (connected)', () => {
  it('reads controls from the relative api/today and stamps commands with the server clock', async () => {
    const today = fixture<TodayView>('today.json')
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(init.body as string)
        return json(202, resultDoc(body.kind, body.idempotency_key, 'accepted'))
      }
      return json(200, today)
    })
    vi.stubGlobal('fetch', fetchMock)
    // the device clock is an hour fast; the command must carry the server's time
    vi.useFakeTimers({ toFake: ['Date'], now: new Date(Date.parse(today.now) + 3_600_000) })
    render(<ControlBar outbox={new CommandOutbox({ newKey: keys(KEY_A) })} />)
    await screen.findByText(/New entries: allowed/)
    expect(fetchMock.mock.calls[0][0]).toBe('api/today')
    fireEvent.click(screen.getByRole('button', { name: LABEL.pause_entries }))
    await screen.findByText(/Sent: Pause new entries/)
    const body = JSON.parse(posts(fetchMock)[0].init.body as string)
    expect(Math.abs(Date.parse(body.issued_at) - Date.parse(today.now))).toBeLessThanOrEqual(5_000)
    // a sent command refreshes the controls
    await waitFor(() => expect(fetchMock.mock.calls.filter(([u]) => u === 'api/today').length).toBe(2))
  })

  it('a missing Today view is unavailable with the HTTP status, never "allowed"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('not found', { status: 404, headers: { 'content-type': 'text/plain' } })),
    )
    render(<ControlBar outbox={new CommandOutbox({ newKey: keys(KEY_A) })} />)
    expect((await screen.findByText(/New entries: unavailable/)).textContent).toContain('404')
    expect(screen.queryByText(/allowed/)).toBeNull()
  })

  it('a response that is not trex-today/1 is unavailable, not a crash', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(200, { plans: [] })))
    render(<ControlBar outbox={new CommandOutbox({ newKey: keys(KEY_A) })} />)
    expect((await screen.findByText(/New entries: unavailable/)).textContent).toContain('trex-today/1')
  })
})
