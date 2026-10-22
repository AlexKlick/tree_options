// Test helpers for the Today and Trade review pages (Wave 3 lane F).
//
// The fixtures are the frozen shared contract files under
// tests/contracts/fixtures (Python round-trips them through its models), so
// every expectation in the lane F tests derives from them, never from the
// code under test. The fetch double routes by the exact relative URL the
// page asked for and records every call (method, URL, body bytes).

import { act } from '@testing-library/react'
import { vi } from 'vitest'
import type { CommandResult, EntryProposalDoc, ProposalList, ProposalRow, TodayView } from './contracts'

const wave3 = import.meta.glob('../../../tests/contracts/fixtures/wave3/*.json', {
  eager: true,
  import: 'default',
})
const wave2 = import.meta.glob('../../../tests/contracts/fixtures/*.json', {
  eager: true,
  import: 'default',
})

function load(from: Record<string, unknown>, name: string): unknown {
  const hit = Object.entries(from).find(([path]) => path.endsWith(`/${name}`))
  if (!hit) throw new Error(`fixture ${name} not found`)
  return JSON.parse(JSON.stringify(hit[1]))
}

export const todayFixture = (): TodayView => load(wave3, 'today.json') as TodayView
export const todayUnavailableFixture = (): TodayView => load(wave3, 'today_unavailable.json') as TodayView
export const proposalListFixture = (): ProposalList => load(wave3, 'wave2_proposal_list.json') as ProposalList
export const appliedResultFixture = (): CommandResult => load(wave2, 'command_result_applied.json') as CommandResult
export const vocabularyFixture = (): Record<string, unknown> => load(wave3, 'vocabulary.json') as Record<string, unknown>

/** The Wave 2 option proposal fixture as a pending `GET /api/proposals` row. */
export function optionRow(proposalSha = 'a'.repeat(64)): ProposalRow {
  const proposal = load(wave2, 'proposal_option.json') as EntryProposalDoc
  return {
    proposal_id: proposal.proposal_id,
    proposal_sha256: proposalSha,
    status: 'pending',
    asset_kind: 'option',
    intent_id: proposal.effect.intent_id,
    proposed_at: proposal.proposed_at,
    send_deadline: proposal.send_deadline,
    proposal,
  }
}

export interface FakeCall {
  url: string
  method: string
  body: string | null
}

/** A Response double: `fetchJson` reads `ok`, `status`, the content type and `json()`. */
export function fakeResponse(status: number, doc: unknown, contentType = 'application/json'): Response {
  const text = typeof doc === 'string' ? doc : JSON.stringify(doc)
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? contentType : null) },
    json: async () => JSON.parse(text),
    text: async () => text,
  } as unknown as Response
}

/** Stub the global `fetch`; the handler answers (or throws for a network drop). */
export function installFetch(handler: (call: FakeCall) => Response | Promise<Response>): FakeCall[] {
  const calls: FakeCall[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call: FakeCall = {
        url: String(input),
        method: init?.method ?? 'GET',
        body: typeof init?.body === 'string' ? init.body : null,
      }
      calls.push(call)
      return handler(call)
    }),
  )
  return calls
}

export const posts = (calls: FakeCall[]): FakeCall[] => calls.filter((c) => c.method === 'POST')

/** Advance the fake clock and let every settled promise commit its state. */
export async function flush(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

/** A `trex-command-result/1` for `body` (the POSTed bytes) with `status`. */
export function resultFor(
  body: string,
  status: CommandResult['status'],
  extra: Partial<CommandResult> = {},
): CommandResult {
  const doc = JSON.parse(body) as { idempotency_key: string; kind: CommandResult['kind'] }
  return {
    schema: 'trex-command-result/1',
    idempotency_key: doc.idempotency_key,
    kind: doc.kind,
    command_body_sha256: 'b'.repeat(64),
    status,
    reason: null,
    detail: '',
    result: null,
    at: '2026-10-22T14:02:05Z',
    ...extra,
  }
}
