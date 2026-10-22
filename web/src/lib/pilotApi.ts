// Research pilot (Wave 4 lane I): GET api/pilot (`trex-pilot-view/1`) and the
// seal_registration command body.
//
// Python is the source of truth: src/tree_options/research/pilot/view.py. The
// fixture tests/research/fixtures/pilot/pilot_view.json is the served view
// (tests/research/test_pilot_view.py pins it); PilotPage.test.tsx checks the
// key lists below against it. The view is lane-local (pending coordinator
// adoption into contracts.ts); it is built only from frozen C3.5 shapes.
//
// Null rule (C3.2): unknown is null with a reason, never zero.

import {
  COMMAND_MAX_TTL_S,
  type BudgetUsage,
  type CommandResult,
  type IdempotencyKey,
  type Instant,
  type PilotRegistration,
  type PilotRole,
  type Sha256Hex,
} from './contracts'

/** Relative: resolves under / (loopback) and /trex/ (portal) alike. */
export const PILOT_VIEW_URL = 'api/pilot'
export const COMMANDS_URL = 'api/commands'

type Known = {
  state: 'draft' | 'sealed'
  role: PilotRole
  unavailable_reason: null
  registration: PilotRegistration
  /** sha256 of the registration body: what the owner reviews and seals. */
  review_sha256: Sha256Hex
}
type Unreadable = {
  state: 'unavailable'
  role: null
  unavailable_reason: string
  registration: null
  review_sha256: null
}
type Usage = { usage: BudgetUsage; usage_unavailable_reason: null } | { usage: null; usage_unavailable_reason: string }

export type PilotProgramRow = {
  program_id: string
  /** Empty: a job of this program would be admitted now. */
  run_blockers: string[]
} & (Known | Unreadable) &
  Usage

export type PilotView = {
  schema: 'trex-pilot-view/1'
  now: Instant
  owners_configured: boolean
  /** True once trex-command/1 carries seal_registration (coordinator amendment). */
  seal_command_available: boolean
  execution_enabled: false
  live_authorized: false
} & (
  | { status: 'ok'; unavailable_reason: null; programs: PilotProgramRow[]; roster_problems: string[] }
  | { status: 'unavailable'; unavailable_reason: string; programs: null; roster_problems: null }
)

export const PILOT_VIEW_KEYS = [
  'schema',
  'now',
  'status',
  'unavailable_reason',
  'programs',
  'roster_problems',
  'owners_configured',
  'seal_command_available',
  'execution_enabled',
  'live_authorized',
] as const satisfies readonly (keyof PilotView)[]

export const PILOT_ROW_KEYS = [
  'program_id',
  'role',
  'state',
  'unavailable_reason',
  'registration',
  'review_sha256',
  'usage',
  'usage_unavailable_reason',
  'run_blockers',
] as const satisfies readonly (keyof PilotProgramRow)[]

export async function getPilot(): Promise<PilotView> {
  const response = await fetch(PILOT_VIEW_URL, { headers: { Accept: 'application/json' } })
  const type = response.headers.get('content-type') ?? ''
  if (!response.ok || !type.includes('application/json')) {
    // the portal's anonymous 403 is an HTML sign-in page: never parsed
    throw new Error(`Pilot registrations unavailable (HTTP ${response.status}). Check your private session and retry.`)
  }
  return (await response.json()) as PilotView
}

// ---------------------------------------------------------------- seal_registration

export interface SealArgs {
  program_id: string
  registration_sha256: Sha256Hex
}
/** A `trex-command/1` body of the `seal_registration` kind (issued_by is
 * stamped by the backend). Mirrors `buildCommandBody`, which cannot name the
 * kind until the coordinator's amendment adds it to `CommandArgs`. */
export interface SealCommandBody {
  schema: 'trex-command/1'
  idempotency_key: IdempotencyKey
  kind: 'seal_registration'
  args: SealArgs
  issued_at: Instant
  expires_at: Instant
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SHA256 = /^[0-9a-f]{64}$/

function instant(at: Date): Instant {
  return at.toISOString().replace(/\.\d{3}Z$/, 'Z')
}

export function sealCommandBody(
  programId: string,
  registrationSha256: Sha256Hex,
  idempotencyKey: IdempotencyKey,
  issuedAt: Date,
): SealCommandBody {
  if (!UUID.test(idempotencyKey)) throw new RangeError('idempotency_key is a lower-case UUID')
  if (!SHA256.test(registrationSha256)) throw new RangeError('registration_sha256 is 64 lower-case hex')
  const issued = new Date(Math.floor(issuedAt.getTime() / 1000) * 1000)
  return {
    schema: 'trex-command/1',
    idempotency_key: idempotencyKey,
    kind: 'seal_registration',
    args: { program_id: programId, registration_sha256: registrationSha256 },
    issued_at: instant(issued),
    expires_at: instant(new Date(issued.getTime() + COMMAND_MAX_TTL_S * 1000)),
  }
}

export type SealAnswer =
  | { outcome: 'queued'; result: CommandResult }
  | { outcome: 'refused'; reason: string; httpStatus: number }
  | { outcome: 'not_sent'; reason: string }

/** POST the seal command. Queued means the web accepted it; the desk owner
 * applies it, and the pilot view shows the sealed record once it has. */
export async function postSeal(body: SealCommandBody): Promise<SealAnswer> {
  let response: Response
  try {
    response = await fetch(COMMANDS_URL, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    return { outcome: 'not_sent', reason: 'network_error' }
  }
  let data: Record<string, unknown> = {}
  if ((response.headers.get('content-type') ?? '').includes('application/json')) {
    data = (await response.json()) as Record<string, unknown>
  }
  if (response.status === 202 || response.status === 200) {
    return { outcome: 'queued', result: data as unknown as CommandResult }
  }
  const reason = String(data.reason ?? data.error ?? `http_${response.status}`)
  return { outcome: 'refused', reason, httpStatus: response.status }
}
