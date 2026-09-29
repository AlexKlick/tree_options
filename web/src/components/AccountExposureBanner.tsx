// Standing account-exposure banner above the desk book panel.
//
// The desk book is the SUPERVISED desk's own book; the same paper account
// also carries the legacy trex books, owned by the trex-monitor service.
// A flat desk book is not a flat account, and this panel used to render
// only the desk book, so an operator could read "flat" while four option
// legs sat in a legacy book with time stops running.
//
// Standing and non-dismissible: no close button, no dismissal state. It is
// red whenever legs exist outside the desk book (naming the owning service
// and the earliest exit deadline) and states plainly when the books could
// not be read — an unreadable book never renders as no exposure.

import type { AccountExposure, AccountExposureSlice } from '../lib/types'

const OWNER_LABEL: Record<string, string> = {
  'trex-monitor': 'trex-monitor (legacy books)',
  'supervised-desk': 'the supervised desk',
}

function ownerLabel(owner: string): string {
  return OWNER_LABEL[owner] ?? owner
}

function slicePhrase(slice: AccountExposureSlice): string {
  if (slice.structures === 0) return 'no exposure'
  const legs = slice.legs === null ? 'leg count unknown' : `${slice.legs} legs`
  const loss = slice.max_loss_usd === null ? 'uncountable' : `$${slice.max_loss_usd}`
  return `${legs} / ${slice.structures} structure${slice.structures === 1 ? '' : 's'} · ${loss} max loss`
}

export function AccountExposureBanner({ exposure }: { exposure: AccountExposure | undefined }) {
  // an older backend serves no block: say nothing rather than claim flat
  if (!exposure) return null
  const outside = exposure.outside_desk_book

  if (!exposure.countable) {
    return (
      <div className="gateway-banner" role="alert" data-testid="account-exposure-banner">
        <strong>⚠ Account exposure unknown</strong>
        <span>
          {outside.structures + exposure.desk_book.structures} structure(s) were read, but a
          book could not be read ({exposure.problems[0] ?? 'no detail'}), so the account total
          is not countable. The desk book is not the account.
        </span>
      </div>
    )
  }

  if (outside.structures === 0) {
    return (
      <div className="gateway-banner gateway-banner-muted" role="status" data-testid="account-exposure-banner">
        <span>
          ○ Account: nothing held outside the desk book — desk book:{' '}
          {slicePhrase(exposure.desk_book)}.
        </span>
      </div>
    )
  }

  const owners = outside.owners.map(ownerLabel).join(', ')
  const deadline = outside.earliest_exit_deadline
  return (
    <div className="gateway-banner" role="alert" data-testid="account-exposure-banner">
      <strong>⚠ This paper account holds {slicePhrase(outside)} OUTSIDE the desk book</strong>
      <span>
        Owned by {owners}
        {deadline ? `; earliest exit ${deadline}` : '; exit deadline unknown'}
        {outside.exit_deadlines_unknown > 0
          ? ` (${outside.exit_deadlines_unknown} structure(s) have no exit deadline on file)`
          : ''}
        . The desk book shows {slicePhrase(exposure.desk_book)}: a flat desk book is not a
        flat account.
      </span>
    </div>
  )
}
