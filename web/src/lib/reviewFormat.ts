// Display helpers for the Today and Trade review pages (Wave 3 lane F):
// integer-cent money, approval countdowns and the owner-facing words for
// every reason code these pages can show.
//
// Reason codes are the server's vocabulary (Wave 2 `Blocker`, the Wave 3
// `Wave3Reason` and `FamilyRefusal`, the loopback authority's refusal
// details, the command store's unavailable reasons) plus the few
// CLIENT_REASONS this browser code names itself. reviewFormat.test.ts pins
// every key below to those Python sources, so no code here is invented. A
// code without words still shows: `reasonText` falls back to the code's
// own words and the pages always print the code beside them.

/** Integer cents as dollars (`26860` -> `$268.60`); money is never a float. */
export function formatCents(cents: number): string {
  if (!Number.isInteger(cents)) throw new RangeError('money is integer cents')
  const sign = cents < 0 ? '-' : ''
  const abs = Math.abs(cents)
  return `${sign}$${Math.floor(abs / 100).toLocaleString('en-US')}.${String(abs % 100).padStart(2, '0')}`
}

/** Whole seconds as `m:ss` (`h:mm:ss` from an hour on), floored at zero. */
export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = String(s % 60).padStart(2, '0')
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

/** Codes this browser code names itself (never sent by a server). */
export const CLIENT_REASONS = [
  'network_error',
  'portal_sign_in_required',
  'unexpected_response',
] as const

export const REASON_TEXT: Readonly<Record<string, string>> = {
  // -- this browser ----------------------------------------------------------
  network_error: 'TREX could not be reached from this device',
  portal_sign_in_required: 'The Family portal answered with a sign-in page; sign in again',
  unexpected_response: 'TREX answered with something this page does not recognise',

  // -- command intake and the command store -----------------------------------
  command_unauthorized: 'TREX did not accept this browser as the owner',
  body_too_large: 'The request was too large',
  invalid_json: 'The request was not valid JSON',
  issued_by_is_stamped_by_the_backend: 'The request named its own issuer, which only the backend may set',
  invalid_command: 'The request was not a valid command',
  command_not_yet_valid: "This device's clock is ahead of the desk clock",
  command_expired: 'The request expired before the desk owner applied it',
  command_body_mismatch: 'This request key was already used for a different command',
  duplicate_command: 'This exact request was already received',
  unknown_command: 'TREX has no record of this request yet',
  command_store_unavailable: "The desk's command folder cannot be used right now",
  commands_dir_unwritable: "The desk's command folder cannot be written",
  command_store_unreadable: "The desk's command folder cannot be read",
  command_store_foreign_file: "An unexpected file sits in the desk's command folder",
  command_result_invalid: 'A stored command result could not be read',
  command_queue_too_long: 'Too many commands are waiting for the desk owner',
  queued_command_invalid: 'A queued command could not be read',
  staged_command_invalid: 'A half-written command could not be read',
  staged_command_mismatch: 'A half-written command does not match its record',
  command_invalid: 'The desk owner could not read the command',
  command_key_mismatch: 'The command file and its key disagree',
  apply_error: 'The desk owner hit an error while applying the command',
  owner_stopped_before_result: 'The desk owner stopped before recording a result',
  gateway_unavailable: 'The desk owner has no broker session yet',

  // -- the loopback authority (this computer) ---------------------------------
  controls_disabled: 'Web commands are switched off on this desk',
  wrong_peer: 'The request did not arrive by the expected local route',
  family_ingress_unavailable: 'Commands through the Family portal are not enabled on this desk',
  forwarded: 'The request came through a proxy, which loopback commands refuse',
  host_invalid: 'The request named an invalid host',
  host_userinfo: 'The request named a host with credentials in it',
  host_not_loopback: 'The request did not name this computer as its host',
  origin_mismatch: 'The page origin did not match the desk',
  cross_site: 'Requests from another site are refused',
  actor_not_bound_to_request: 'The verified owner was not bound to this exact request',

  // -- the Family portal assertion (FamilyRefusal, in check order) ------------
  family_ingress_disabled: 'Commands through the Family portal are not configured on this desk',
  assertion_missing: 'The Family portal did not sign this request',
  assertion_malformed: "The Family portal's signature was malformed",
  assertion_bad_signature: "The Family portal's signature did not verify",
  assertion_claims_invalid: "The Family portal's signed claims were invalid",
  assertion_wrong_audience: 'The signature was made for another TREX instance',
  assertion_not_yet_valid: 'The signature is dated in the future',
  assertion_expired: 'The signature expired before TREX checked it; try again',
  assertion_method_mismatch: 'The signature was made for another kind of request',
  assertion_path_mismatch: 'The signature was made for another address',
  assertion_origin_not_allowed: 'This site is not an allowed origin for TREX commands',
  assertion_origin_mismatch: 'The signed origin did not match the request',
  assertion_host_mismatch: 'The signed origin did not match the host',
  assertion_body_mismatch: 'The request changed after the portal signed it',
  assertion_request_id_mismatch: 'The signed request key did not match the request',
  assertion_sub_not_owner: 'This account is not a TREX owner',
  assertion_replayed: 'This signed request was already used; try again',
  assertion_ledger_unavailable: "TREX could not record the signature's single use",

  // -- approvals, entries and the desk ----------------------------------------
  approval_expired: 'The approval window closed',
  approval_epoch_mismatch: 'The desk owner restarted after this proposal was made; it needs a new proposal',
  approval_profile_mismatch: 'The risk profile changed after this proposal was made',
  approval_effect_mismatch: 'The order differs from the one proposed',
  approval_proposal_mismatch: 'The approval does not match the stored proposal',
  mandate_already_consumed: 'This entry was already used',
  entry_mandate_already_granted: 'This entry was already approved',
  proposal_unknown: 'The desk does not know this proposal',
  proposal_rejected: 'This proposal was already rejected',
  intent_already_used: 'This trade intent was already used',
  account_unbound: 'No paper account is bound to the desk',
  entries_paused: 'New entries are paused',
  halt: 'The emergency stop is standing',
  flatten: 'Close all desk positions is standing',
  auto_off: 'Automatic trading is off',
  exit_owner_not_ready: 'No exit owner is watching open positions',
  entry_window_closed: 'The entry window is closed',
  outside_regular_session: 'The market is outside regular hours',
  session_entry_limit_reached: "Today's entry limit is reached",
  reconciliation_required: "The desk's records and the broker disagree; reconcile first",
  risk_snapshot_stale: 'The risk snapshot is too old',
  insufficient_cash: 'Not enough settled cash',
  equity_quote_not_realtime: 'The equity quote is delayed, not real time',
  equity_symbol_not_qualified: 'This stock is not qualified for the desk',
  quote_stale_or_future: 'The quote is too old (or dated in the future)',
  quote_unavailable: 'No quote is available',
  broker_owner_unhealthy: 'The broker session owner is unhealthy',
  gateway_disconnected: 'IB Gateway is disconnected',
  trade_loss_cap_exceeded: "The trade's maximum loss is over the per-trade cap",
  open_loss_cap_exceeded: 'Open loss would exceed its cap',
  open_exposure_unknown: 'Open exposure is unknown',
  daily_loss_unknown: "Today's realized loss is unknown",
  daily_loss_cap_exceeded: "Today's loss cap is reached",
  trade_exceeds_intended_capital: 'The trade needs more than its intended capital',
  equity_notional_exceeds_intended_capital: 'The stock order needs more than its intended capital',
  protective_exit_unavailable: 'No protective exit can be guaranteed',
  profile_absent: 'No risk profile is configured',
  profile_invalid: 'The risk profile is invalid',
  profile_limits_missing: 'The risk profile has no limits',
  broker_rejected: 'The broker rejected the order',
  send_uncertain: 'It is uncertain whether the order reached the broker',

  // -- views: why a block is unknown ------------------------------------------
  calendar_unreadable: 'The market calendar could not be read',
  calendar_exhausted: 'The market calendar has no more sessions',
  heartbeat_absent: "The desk owner's heartbeat is missing",
  runtime_lock_unknown: "Whether the desk owner holds its lock is unknown",
  gateway_watch_not_reporting: 'The IB Gateway watch is not reporting',
  gateway_watch_stale: "The IB Gateway watch's last report is too old",
  kill_file_unreadable: 'The kill-switch files could not be read',
  proposals_dir_unreadable: 'The proposals folder could not be read',
  acceptance_missing: 'No acceptance document has been computed yet',
  acceptance_invalid: 'The acceptance document is invalid',
  acceptance_calendar_mismatch: 'The acceptance document was judged against another calendar',
  acceptance_window_mismatch: 'The acceptance document covers the wrong sessions',
  acceptance_owners_mismatch: 'The acceptance document names other owners than this desk',
  schedule_unavailable: 'The schedule could not be computed',
  activity_source_unreadable: 'An activity source could not be read',
  actor_not_recorded: 'Who did this was not recorded',
  desk_owner_stopped: 'The desk owner is stopped',
  desk_owner_stale: "The desk owner's heartbeat is stale",
  gateway_needs_login: 'IB Gateway needs a login',
  gateway_needs_2fa: 'IB Gateway is waiting for two-factor approval',
  gateway_starting: 'IB Gateway is starting',
  gateway_api_down: "IB Gateway's API is not answering",
  gateway_down: 'IB Gateway is down',
  exit_unguarded: 'An open position has no armed exit owner',
  calendar_horizon_near: 'The market calendar is close to running out',
  family_commands_disabled: 'Commands through the Family portal are disabled',
  holdings_missing: 'No holdings snapshot exists yet',
  holdings_unreadable: 'The holdings snapshot could not be read',
  holdings_invalid: 'The holdings snapshot is invalid',
  risk_snapshot_missing: 'No risk snapshot exists yet',
  risk_snapshot_unreadable: 'The risk snapshot could not be read',
  risk_snapshot_invalid: 'The risk snapshot is invalid',
  risk_snapshot_unavailable: 'The risk snapshot is unavailable',
  reconciliation_missing: 'No reconciliation has run yet',
  reconciliation_unreadable: 'The reconciliation could not be read',
  reconciliation_invalid: 'The reconciliation is invalid',
  reconciliation_stale: 'The reconciliation is too old',
  reconciliation_unavailable: 'The reconciliation is unavailable',
  proposal_unreadable: 'The proposal file could not be read',
  proposal_invalid: 'The proposal file is invalid',
  proposal_not_canonical: 'The proposal file is not in canonical form',
  proposal_id_mismatch: "The proposal file's name does not match its id",
  mandate_unreadable: 'The approval record could not be read',
}

/** The owner-facing words for a reason code (the code's own words if unknown). */
export function reasonText(code: string): string {
  return REASON_TEXT[code] ?? code.replace(/[_:]+/g, ' ').trim()
}
