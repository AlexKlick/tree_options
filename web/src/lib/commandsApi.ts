// Browser commands and reads for the Today and Trade review pages (Wave 3
// lane F; docs/product/WAVE3-CONTRACTS.md, Wave 2 `trex-command/1`).
//
// The web only QUEUES commands: `POST api/commands` stores a
// `trex-command/1` body for the desk owner, which applies it and records
// the outcome that `GET api/commands/{key}` answers. Three rules hold here:
//
// - One decision is one body. `prepareCommand` freezes the key, the issue
//   time and the bytes; every retry re-sends those exact bytes, so a retry
//   can only ever be the store's `duplicate` (same key, same body), never a
//   second command.
// - Instants come from the desk's clock: a body is issued at the response's
//   `now` plus the time since that response arrived (`serverNowMs`), never
//   at this device's clock, which may be skewed.
// - URLs are relative (no leading slash): the app is served at `/` on
//   loopback and at `/trex/` through the Family portal.
//
// Nothing here approves anything by itself: a command is sent only from an
// explicit owner action (`useCommand().submit`).

import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchJson } from './api'
import {
  API_PATHS,
  COMMAND_MAX_TTL_S,
  buildCommandBody,
  type ApproveEntryArgs,
  type CommandArgs,
  type CommandBody,
  type CommandKind,
  type CommandResult,
  type CommandStatus,
  type IdempotencyKey,
  type Instant,
  type ProposalList,
  type ProposalRow,
  type TodayView,
} from './contracts'

// ---------------------------------------------------------------- URLs

/** A contract API path relative to the app's mount. */
export function relativeUrl(path: string): string {
  return path.replace(/^\/+/, '')
}

export const COMMANDS_URL = relativeUrl(API_PATHS.commands)
export const TODAY_URL = relativeUrl(API_PATHS.today)
export const PROPOSALS_URL = relativeUrl(API_PATHS.proposals)

export function commandResultUrl(key: IdempotencyKey): string {
  return `${COMMANDS_URL}/${key}`
}

export const getToday = (): Promise<TodayView> => fetchJson<TodayView>(TODAY_URL)
export const getProposals = (): Promise<ProposalList> => fetchJson<ProposalList>(PROPOSALS_URL)

// ---------------------------------------------------------------- keys and clocks

/** A lower-case version 4 UUID (`getRandomValues` works outside secure contexts too). */
export function newIdempotencyKey(
  fill: (bytes: Uint8Array) => Uint8Array = (bytes) => crypto.getRandomValues(bytes),
): IdempotencyKey {
  const b = fill(new Uint8Array(16))
  b[6] = (b[6] & 0x0f) | 0x40
  b[8] = (b[8] & 0x3f) | 0x80
  const hex = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

/** The desk's clock now: the response's `now` plus the time since it arrived. */
export function serverNowMs(responseNow: Instant, receivedAtMs: number, clientNowMs: number): number {
  return Date.parse(responseNow) + Math.max(0, clientNowMs - receivedAtMs)
}

/** Whole seconds from `now` to `deadline` (both server instants), floored at zero. */
export function secondsUntil(deadline: Instant, now: Instant): number {
  return Math.max(0, Math.floor((Date.parse(deadline) - Date.parse(now)) / 1000))
}

// ---------------------------------------------------------------- approvals

/** The `approve_entry` args for a `GET /api/proposals` row: the hashes and the
 * owner epoch and profile digest the proposal was reviewed under. */
export function approveArgsFromRow(row: ProposalRow): ApproveEntryArgs {
  return {
    proposal_id: row.proposal_id,
    proposal_sha256: row.proposal_sha256,
    effect_sha256: row.proposal.effect_sha256,
    owner_epoch: row.proposal.review.owner_epoch,
    profile_digest: row.proposal.review.profile_digest,
  }
}

/** An approval command lives at most 300 s and never past its approval window. */
export function approvalTtlS(secondsLeftOnApproval: number): number {
  if (!Number.isInteger(secondsLeftOnApproval) || secondsLeftOnApproval < 1) {
    throw new RangeError('the approval window has closed')
  }
  return Math.min(COMMAND_MAX_TTL_S, secondsLeftOnApproval)
}

// ---------------------------------------------------------------- one command

export interface PreparedCommand<K extends CommandKind = CommandKind> {
  key: IdempotencyKey
  kind: K
  body: CommandBody<K>
  /** The exact bytes POSTed; every retry re-sends them unchanged. */
  text: string
  ttlS: number
}

export function prepareCommand<K extends CommandKind>(
  kind: K,
  args: CommandArgs[K],
  issuedAtMs: number,
  ttlS: number = COMMAND_MAX_TTL_S,
  key: IdempotencyKey = newIdempotencyKey(),
): PreparedCommand<K> {
  const body = buildCommandBody(kind, args, key, new Date(issuedAtMs), ttlS)
  return { key, kind, body, text: JSON.stringify(body), ttlS }
}

export interface CommandError {
  type: 'error'
  /** The HTTP status, or null when no response arrived. */
  http: number | null
  /** The answer's `error` (or a CLIENT_REASONS code). */
  code: string
  /** A more specific vocabulary code (the refusal `detail` or `reason`), if any. */
  cause: string | null
  /** Free text the server or the network gave, if any. */
  detail: string
  /** True when re-sending the same bytes may succeed. */
  retryable: boolean
}
export type CommandOutcome = { type: 'result'; http: number; result: CommandResult } | CommandError

/** Statuses a 4xx/5xx answer can carry that a later identical try may pass:
 * an expired portal signature or session, a store fault, a gateway hiccup. */
const RETRYABLE_HTTP: ReadonlySet<number> = new Set([401, 403, 404, 408, 425, 429, 500, 502, 503, 504])
const CODE = /^[a-z0-9_:]+$/

type Fetch = (input: string, init?: RequestInit) => Promise<Response>
const defaultFetch: Fetch = (input, init) => fetch(input, init)

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isResultFor(doc: unknown, key: IdempotencyKey): doc is CommandResult {
  return (
    isRecord(doc) &&
    doc.schema === 'trex-command-result/1' &&
    doc.idempotency_key === key &&
    typeof doc.status === 'string'
  )
}

async function readJson(response: Response): Promise<unknown> {
  if (!(response.headers.get('content-type') ?? '').includes('application/json')) return undefined
  try {
    return await response.json()
  } catch {
    return undefined
  }
}

async function interpret(response: Response, key: IdempotencyKey): Promise<CommandOutcome> {
  const doc = await readJson(response)
  if (isResultFor(doc, key)) return { type: 'result', http: response.status, result: doc }
  const retryable = RETRYABLE_HTTP.has(response.status)
  if (doc === undefined) {
    const code = response.status === 401 || response.status === 403 ? 'portal_sign_in_required' : 'unexpected_response'
    return { type: 'error', http: response.status, code, cause: null, detail: '', retryable: true }
  }
  if (!isRecord(doc) || typeof doc.error !== 'string' || response.ok) {
    return { type: 'error', http: response.status, code: 'unexpected_response', cause: null, detail: '', retryable: true }
  }
  const code = doc.error
  const ids = [doc.detail, doc.reason].filter((v): v is string => typeof v === 'string' && CODE.test(v) && v !== code)
  const detail = typeof doc.detail === 'string' && !CODE.test(doc.detail) ? doc.detail : ''
  return { type: 'error', http: response.status, code, cause: ids[0] ?? null, detail, retryable }
}

function networkError(err: unknown): CommandError {
  const detail = err instanceof Error ? err.message : String(err)
  return { type: 'error', http: null, code: 'network_error', cause: null, detail, retryable: true }
}

/** POST the prepared bytes; a refusal at intake (409) is a result, not an error. */
export async function postCommand(prepared: PreparedCommand, fetchImpl: Fetch = defaultFetch): Promise<CommandOutcome> {
  let response: Response
  try {
    response = await fetchImpl(COMMANDS_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: prepared.text,
      credentials: 'same-origin',
      cache: 'no-store',
    })
  } catch (err) {
    return networkError(err)
  }
  return interpret(response, prepared.key)
}

/** `GET api/commands/{key}`: the stored (latest) result. */
export async function fetchCommandResult(key: IdempotencyKey, fetchImpl: Fetch = defaultFetch): Promise<CommandOutcome> {
  let response: Response
  try {
    response = await fetchImpl(commandResultUrl(key), {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
      cache: 'no-store',
    })
  } catch (err) {
    return networkError(err)
  }
  return interpret(response, key)
}

export const TERMINAL_STATUSES: ReadonlySet<CommandStatus> = new Set(['applied', 'refused', 'failed'])

/** A result as the owner should read it: a `duplicate` stands for the first
 * outcome it names (null when that outcome is not readable). */
export function effectiveResult(result: CommandResult): CommandResult | null {
  if (result.status !== 'duplicate') return result
  const first = result.result?.first_result
  return isResultFor(first, result.idempotency_key) ? first : null
}

// ---------------------------------------------------------------- the hook

export type CommandPhase =
  | { name: 'idle' }
  | { name: 'sending'; prepared: PreparedCommand; attempt: number }
  /** Queued (`accepted`): waiting for the desk owner to apply it. */
  | { name: 'waiting'; prepared: PreparedCommand; result: CommandResult }
  /** `applied`, `refused` or `failed`: the final word. */
  | { name: 'settled'; prepared: PreparedCommand; result: CommandResult }
  | { name: 'error'; prepared: PreparedCommand; error: CommandError; attempt: number }
  /** No final word before the command expired (plus a grace minute). */
  | { name: 'unconfirmed'; prepared: PreparedCommand; result: CommandResult | null }

export interface CommandRunner {
  phase: CommandPhase
  /** Send a prepared command (ignored while one is being sent or awaited). */
  submit: (prepared: PreparedCommand) => void
  /** Re-send the SAME prepared bytes after an error. */
  retry: () => void
  /** One more look at the stored result after giving up waiting. */
  checkAgain: () => void
  /** Forget the current command (the next submit is a new request). */
  reset: () => void
}

export interface UseCommandOptions {
  /** Result polling interval while the desk owner has not answered. */
  pollMs?: number
  /** Called once with the final (effective) result. */
  onSettled?: (result: CommandResult) => void
  fetchImpl?: Fetch
}

/** After expiry the owner still records a refusal; wait this long for it. */
export const RESULT_GRACE_S = 60

export function useCommand(options: UseCommandOptions = {}): CommandRunner {
  const pollMs = options.pollMs ?? 2000
  const [phase, setPhase] = useState<CommandPhase>({ name: 'idle' })
  const optionsRef = useRef(options)
  optionsRef.current = options
  const phaseRef = useRef<CommandPhase>(phase)
  phaseRef.current = phase
  const alive = useRef(true)
  const busy = useRef(false)
  const timer = useRef<number | null>(null)
  const generation = useRef(0)

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      if (timer.current !== null) window.clearTimeout(timer.current)
    }
  }, [])

  const fetchFn = (): Fetch => optionsRef.current.fetchImpl ?? defaultFetch

  const settle = useCallback((prepared: PreparedCommand, result: CommandResult) => {
    setPhase({ name: 'settled', prepared, result })
    optionsRef.current.onSettled?.(result)
  }, [])

  const poll = useCallback(
    async (gen: number, prepared: PreparedCommand, giveUpAtMs: number, last: CommandResult | null) => {
      timer.current = null
      busy.current = true
      const outcome = await fetchCommandResult(prepared.key, fetchFn())
      if (!alive.current || gen !== generation.current) return
      busy.current = false
      let latest = last
      if (outcome.type === 'result') {
        const result = effectiveResult(outcome.result) ?? outcome.result
        if (TERMINAL_STATUSES.has(result.status)) {
          settle(prepared, result)
          return
        }
        latest = result
        setPhase({ name: 'waiting', prepared, result })
      }
      if (Date.now() >= giveUpAtMs) {
        setPhase({ name: 'unconfirmed', prepared, result: latest })
        return
      }
      busy.current = true
      timer.current = window.setTimeout(() => void poll(gen, prepared, giveUpAtMs, latest), pollMs)
    },
    // fetchFn reads a ref; settle is stable
    [pollMs, settle],
  )

  const send = useCallback(
    async (prepared: PreparedCommand, attempt: number) => {
      if (busy.current) return
      busy.current = true
      const gen = ++generation.current
      setPhase({ name: 'sending', prepared, attempt })
      const outcome = await postCommand(prepared, fetchFn())
      if (!alive.current || gen !== generation.current) return
      busy.current = false
      if (outcome.type === 'error') {
        setPhase({ name: 'error', prepared, error: outcome, attempt })
        return
      }
      const effective = effectiveResult(outcome.result)
      if (effective !== null && TERMINAL_STATUSES.has(effective.status)) {
        settle(prepared, effective)
        return
      }
      const shown = effective ?? outcome.result
      setPhase({ name: 'waiting', prepared, result: shown })
      busy.current = true
      const giveUpAtMs = Date.now() + (prepared.ttlS + RESULT_GRACE_S) * 1000
      timer.current = window.setTimeout(() => void poll(gen, prepared, giveUpAtMs, shown), pollMs)
    },
    [poll, pollMs, settle],
  )

  const submit = useCallback((prepared: PreparedCommand) => void send(prepared, 1), [send])

  const retry = useCallback(() => {
    const current = phaseRef.current
    if (current.name === 'error') void send(current.prepared, current.attempt + 1)
  }, [send])

  const checkAgain = useCallback(() => {
    const current = phaseRef.current
    if (current.name !== 'unconfirmed' || busy.current) return
    void poll(++generation.current, current.prepared, Date.now(), current.result)
  }, [poll])

  const reset = useCallback(() => {
    generation.current += 1
    busy.current = false
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = null
    setPhase({ name: 'idle' })
  }, [])

  return { phase, submit, retry, checkAgain, reset }
}
