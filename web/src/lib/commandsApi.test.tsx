import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { KEYS, type CommandResult } from './contracts'
import {
  COMMANDS_URL,
  PROPOSALS_URL,
  TODAY_URL,
  approvalTtlS,
  approveArgsFromRow,
  commandResultUrl,
  effectiveResult,
  fetchCommandResult,
  newIdempotencyKey,
  postCommand,
  prepareCommand,
  secondsUntil,
  serverNowMs,
  useCommand,
  type PreparedCommand,
} from './commandsApi'
import {
  appliedResultFixture,
  fakeResponse,
  flush,
  installFetch,
  posts,
  proposalListFixture,
  resultFor,
  todayFixture,
  vocabularyFixture,
} from './reviewTestKit'

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const KEY = '6f1c2b8e-3d4a-4f5b-9c6d-7e8f9a0b1c2d'

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('relative URLs (the app is mounted at / and at /trex/)', () => {
  it('every command and read URL is the contract path without its leading slash', () => {
    const paths = vocabularyFixture().api_paths as Record<string, string>
    expect(COMMANDS_URL).toBe(paths.commands.slice(1))
    expect(TODAY_URL).toBe(paths.today.slice(1))
    expect(PROPOSALS_URL).toBe(paths.proposals.slice(1))
    expect(commandResultUrl(KEY)).toBe(`api/commands/${KEY}`)
    for (const url of [COMMANDS_URL, TODAY_URL, PROPOSALS_URL, commandResultUrl(KEY)]) {
      expect(url.startsWith('/')).toBe(false)
    }
  })
})

describe('idempotency keys', () => {
  it('builds a lower-case version 4 UUID from 16 random bytes', () => {
    // bytes 00..0f; byte 6 -> 0x46 (version 4), byte 8 -> 0x88 (variant 10xx)
    const fixed = newIdempotencyKey((bytes) => {
      bytes.forEach((_, i) => (bytes[i] = i))
      return bytes
    })
    expect(fixed).toBe('00010203-0405-4607-8809-0a0b0c0d0e0f')
    const a = newIdempotencyKey()
    const b = newIdempotencyKey()
    expect(a).toMatch(UUID_V4)
    expect(b).toMatch(UUID_V4)
    expect(a).not.toBe(b)
  })
})

describe('the server clock', () => {
  it('is the response now plus the time since the response arrived', () => {
    expect(serverNowMs('2026-10-22T14:02:00Z', 1_000, 4_500)).toBe(Date.UTC(2026, 9, 22, 14, 2, 3, 500))
    // a client clock that steps backwards never moves the server clock back
    expect(serverNowMs('2026-10-22T14:02:00Z', 9_000, 4_500)).toBe(Date.UTC(2026, 9, 22, 14, 2, 0))
  })

  it('seconds until a proposal deadline agree with the Today view at the same now', () => {
    const list = proposalListFixture()
    const today = todayFixture()
    const item = today.approvals.items![0]
    expect(list.now).toBe(today.now)
    expect(secondsUntil(list.proposals[0].send_deadline, list.now)).toBe(item.seconds_to_deadline)
    expect(secondsUntil('2026-10-22T14:00:00Z', list.now)).toBe(0)
  })
})

describe('approve args', () => {
  it('a proposal row yields exactly the args the Today view repeats verbatim', () => {
    const row = proposalListFixture().proposals[0]
    const expected = todayFixture().approvals.items![0].approve_args
    const args = approveArgsFromRow(row)
    expect(args).toEqual(expected)
    expect(Object.keys(args).sort()).toEqual([...KEYS.ApproveEntryArgs].sort())
  })

  it('an approval command never outlives its approval window', () => {
    expect(approvalTtlS(480)).toBe(300)
    expect(approvalTtlS(300)).toBe(300)
    expect(approvalTtlS(42)).toBe(42)
    expect(() => approvalTtlS(0)).toThrow(RangeError)
  })
})

describe('prepareCommand', () => {
  it('freezes one body: whole-second issue time, TTL and the exact bytes to send', () => {
    const args = todayFixture().approvals.items![0].approve_args
    const prepared = prepareCommand('approve_entry', args, Date.UTC(2026, 9, 22, 14, 2, 3, 900), 300, KEY)
    expect(prepared.key).toBe(KEY)
    expect(prepared.body.issued_at).toBe('2026-10-22T14:02:03Z')
    expect(prepared.body.expires_at).toBe('2026-10-22T14:07:03Z')
    expect(prepared.text).toBe(JSON.stringify(prepared.body))
    const sent = JSON.parse(prepared.text) as Record<string, unknown>
    expect(Object.keys(sent).sort()).toEqual([...KEYS.CommandBody].sort())
    expect(sent.args).toEqual(args)
    expect(sent).not.toHaveProperty('issued_by')
    expect(prepareCommand('pause_entries', {}, Date.UTC(2026, 9, 22, 14, 2, 3)).key).toMatch(UUID_V4)
  })
})

function prepared(kind: 'approve_entry' | 'pause_entries' = 'pause_entries', ttl = 300): PreparedCommand {
  const args = kind === 'approve_entry' ? todayFixture().approvals.items![0].approve_args : { reason: 'test' }
  return prepareCommand(kind, args, Date.UTC(2026, 9, 22, 14, 2, 3), ttl, KEY)
}

describe('postCommand', () => {
  it('POSTs the prepared bytes to the relative commands URL and returns the result', async () => {
    const command = prepared('approve_entry')
    const calls = installFetch((call) => fakeResponse(202, resultFor(call.body!, 'accepted')))
    const outcome = await postCommand(command)
    expect(calls).toEqual([{ url: 'api/commands', method: 'POST', body: command.text }])
    expect(outcome).toMatchObject({ type: 'result', http: 202, result: { status: 'accepted', idempotency_key: KEY } })
  })

  it('an intake refusal (409) is a result, not an error', async () => {
    const command = prepared()
    installFetch((call) => fakeResponse(409, resultFor(call.body!, 'refused', { reason: 'command_expired' })))
    expect(await postCommand(command)).toMatchObject({ type: 'result', http: 409, result: { reason: 'command_expired' } })
  })

  it('an authority refusal names the most specific vocabulary code', async () => {
    installFetch(() =>
      fakeResponse(403, { error: 'command_unauthorized', reason: 'command_unauthorized', detail: 'assertion_sub_not_owner' }),
    )
    expect(await postCommand(prepared())).toEqual({
      type: 'error',
      http: 403,
      code: 'command_unauthorized',
      cause: 'assertion_sub_not_owner',
      detail: '',
      retryable: true,
    })
  })

  it('a store fault is retryable; a malformed command is not', async () => {
    installFetch(() => fakeResponse(503, { error: 'command_store_unavailable', reason: 'commands_dir_unwritable' }))
    expect(await postCommand(prepared())).toMatchObject({
      code: 'command_store_unavailable',
      cause: 'commands_dir_unwritable',
      retryable: true,
    })
    installFetch(() => fakeResponse(422, { error: 'invalid_command', errors: [{ loc: ['args'], type: 'x', msg: 'y' }] }))
    expect(await postCommand(prepared())).toMatchObject({ http: 422, code: 'invalid_command', cause: null, retryable: false })
  })

  it('a network drop, a portal sign-in page and a foreign result are retryable errors', async () => {
    installFetch(() => {
      throw new TypeError('Failed to fetch')
    })
    expect(await postCommand(prepared())).toMatchObject({ http: null, code: 'network_error', retryable: true })
    installFetch(() => fakeResponse(403, '<html>Sign in</html>', 'text/html'))
    expect(await postCommand(prepared())).toMatchObject({ http: 403, code: 'portal_sign_in_required', retryable: true })
    const foreign = { ...appliedResultFixture(), idempotency_key: '00000000-0000-4000-8000-000000000000' }
    installFetch(() => fakeResponse(202, foreign))
    expect(await postCommand(prepared())).toMatchObject({ code: 'unexpected_response', retryable: true })
  })
})

describe('result polling and duplicates', () => {
  it('GET api/commands/{key} answers the stored result', async () => {
    const applied = { ...appliedResultFixture(), idempotency_key: KEY }
    const calls = installFetch(() => fakeResponse(200, applied))
    expect(await fetchCommandResult(KEY)).toEqual({ type: 'result', http: 200, result: applied })
    expect(calls).toEqual([{ url: `api/commands/${KEY}`, method: 'GET', body: null }])
    installFetch(() => fakeResponse(404, { error: 'unknown_command' }))
    expect(await fetchCommandResult(KEY)).toMatchObject({ code: 'unknown_command', retryable: true })
  })

  it('a duplicate stands for the first outcome it names', () => {
    const first = { ...appliedResultFixture(), idempotency_key: KEY }
    const duplicate: CommandResult = {
      ...first,
      status: 'duplicate',
      reason: 'duplicate_command',
      result: { first_status: 'applied', first_result: first as unknown as Record<string, unknown> },
    }
    expect(effectiveResult(duplicate)).toEqual(first)
    expect(effectiveResult(first)).toEqual(first)
    expect(effectiveResult({ ...duplicate, result: null })).toBeNull()
  })
})

function Harness({ command, onSettled }: { command: PreparedCommand; onSettled?: (r: CommandResult) => void }) {
  const runner = useCommand({ pollMs: 1000, onSettled })
  const phase = runner.phase
  return (
    <div>
      <p data-testid="phase">{phase.name}</p>
      <p data-testid="status">{'result' in phase && phase.result ? phase.result.status : ''}</p>
      <button onClick={() => runner.submit(command)}>submit</button>
      <button onClick={runner.retry}>retry</button>
      <button onClick={runner.checkAgain}>check</button>
    </div>
  )
}

describe('useCommand', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(Date.UTC(2026, 9, 22, 14, 2, 3))
  })

  const phase = () => screen.getByTestId('phase').textContent
  const status = () => screen.getByTestId('status').textContent

  it('a retry after a network drop re-sends the same bytes under the same key', async () => {
    const command = prepared('approve_entry')
    let drop = true
    const calls = installFetch((call) => {
      if (call.method === 'POST' && drop) {
        drop = false
        throw new TypeError('Failed to fetch')
      }
      return fakeResponse(202, resultFor(command.text, 'accepted'))
    })
    render(<Harness command={command} />)
    fireEvent.click(screen.getByText('submit'))
    await flush()
    expect(phase()).toBe('error')
    fireEvent.click(screen.getByText('retry'))
    await flush()
    expect(phase()).toBe('waiting')
    const sent = posts(calls)
    expect(sent).toHaveLength(2)
    expect(sent[1].body).toBe(sent[0].body)
    expect(JSON.parse(sent[1].body!).idempotency_key).toBe(KEY)
  })

  it('a double submit sends one command', async () => {
    const command = prepared()
    const calls = installFetch(() => fakeResponse(202, resultFor(command.text, 'accepted')))
    render(<Harness command={command} />)
    fireEvent.click(screen.getByText('submit'))
    fireEvent.click(screen.getByText('submit'))
    await flush()
    expect(posts(calls)).toHaveLength(1)
  })

  it('polls the stored result until the desk owner applies it, then stops', async () => {
    const command = prepared()
    const onSettled = vi.fn()
    let checks = 0
    const calls = installFetch((call) => {
      if (call.method === 'POST') return fakeResponse(202, resultFor(command.text, 'accepted'))
      checks += 1
      return fakeResponse(200, resultFor(command.text, checks < 3 ? 'accepted' : 'applied'))
    })
    render(<Harness command={command} onSettled={onSettled} />)
    fireEvent.click(screen.getByText('submit'))
    await flush()
    expect(phase()).toBe('waiting')
    await flush(1000)
    await flush(1000)
    expect(phase()).toBe('waiting')
    await flush(1000)
    expect(phase()).toBe('settled')
    expect(status()).toBe('applied')
    expect(onSettled).toHaveBeenCalledTimes(1)
    await flush(10_000)
    const gets = calls.filter((c) => c.method === 'GET')
    expect(gets).toHaveLength(3)
    expect(gets.every((c) => c.url === `api/commands/${KEY}`)).toBe(true)
  })

  it('a duplicate of an applied command and an intake refusal settle without polling', async () => {
    const command = prepared()
    const first = resultFor(command.text, 'applied')
    let calls = installFetch(() =>
      fakeResponse(
        200,
        resultFor(command.text, 'duplicate', {
          reason: 'duplicate_command',
          result: { first_status: 'applied', first_result: first as unknown as Record<string, unknown> },
        }),
      ),
    )
    const view = render(<Harness command={command} />)
    fireEvent.click(screen.getByText('submit'))
    await flush(5000)
    expect([phase(), status()]).toEqual(['settled', 'applied'])
    expect(calls.filter((c) => c.method === 'GET')).toHaveLength(0)
    view.unmount()

    calls = installFetch(() => fakeResponse(409, resultFor(command.text, 'refused', { reason: 'command_expired' })))
    render(<Harness command={command} />)
    fireEvent.click(screen.getByText('submit'))
    await flush(5000)
    expect([phase(), status()]).toEqual(['settled', 'refused'])
    expect(calls.filter((c) => c.method === 'GET')).toHaveLength(0)
  })

  it('gives up waiting after the command TTL plus a minute, and can check once more', async () => {
    const command = prepared('pause_entries', 30)
    const calls = installFetch((call) =>
      fakeResponse(call.method === 'POST' ? 202 : 200, resultFor(command.text, 'accepted')),
    )
    render(<Harness command={command} />)
    fireEvent.click(screen.getByText('submit'))
    await flush()
    await flush(89_000)
    expect(phase()).toBe('waiting')
    await flush(2_000)
    expect(phase()).toBe('unconfirmed')
    const before = calls.length
    await flush(10_000)
    expect(calls.length).toBe(before)
    fireEvent.click(screen.getByText('check'))
    await flush()
    expect(calls.length).toBe(before + 1)
    expect(phase()).toBe('unconfirmed')
  })
})
