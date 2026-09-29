import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { AccountExposure, AccountExposureSlice } from '../lib/types'
import { AccountExposureBanner } from './AccountExposureBanner'

afterEach(cleanup)

const emptySlice: AccountExposureSlice = {
  structures: 0, legs: 0, max_loss_usd: '0.00', earliest_exit_deadline: null,
  exit_deadlines_unknown: 0, owners: [], books: [], positions: [],
}

function exposure(over: Partial<AccountExposure> = {}): AccountExposure {
  return {
    schema: 'desk-account-exposure/1', as_of: '2026-09-30',
    state_root: '/home/x/.local/state/trex', desk_run_dir: '/home/x/.local/state/trex/desk-paper',
    countable: true, max_loss_usd: '0.00', problems: [],
    desk_book: emptySlice, outside_desk_book: emptySlice, ...over,
  }
}

// The live account (2026-09-29): two NVDA put spreads in a legacy book,
// $105 + $372, the desk book flat. The desk panel alone read "flat".
const live: AccountExposure = exposure({
  max_loss_usd: '477.00',
  desk_book: { ...emptySlice, max_loss_usd: '0.00' },
  outside_desk_book: {
    structures: 2, legs: 4, max_loss_usd: '477.00',
    earliest_exit_deadline: '2026-10-09', exit_deadlines_unknown: 0,
    owners: ['trex-monitor'], books: ['legacy:putspread-20260922'],
    positions: [
      { id: 'legacy:putspread-20260922/nvda-oct', book: 'legacy:putspread-20260922',
        owner: 'trex-monitor', underlying: 'NVDA', status: 'open', quantity: 5,
        max_loss_usd: '105.00', exit_deadline: '2026-10-09' },
      { id: 'legacy:putspread-20260922/nvda-nov', book: 'legacy:putspread-20260922',
        owner: 'trex-monitor', underlying: 'NVDA', status: 'open', quantity: 3,
        max_loss_usd: '372.00', exit_deadline: '2026-11-06' },
    ],
  },
})

describe('AccountExposureBanner', () => {
  it('is red and names the owner and the earliest deadline when legs sit outside the desk book', () => {
    render(<AccountExposureBanner exposure={live} />)
    const banner = screen.getByRole('alert')
    const text = banner.textContent ?? ''
    expect(text).toMatch(/OUTSIDE the desk book/)
    expect(text).toMatch(/4 legs \/ 2 structures/)
    expect(text).toMatch(/\$477\.00 max loss/)
    expect(text).toMatch(/trex-monitor/)
    expect(text).toMatch(/earliest exit 2026-10-09/)
    expect(text).toMatch(/flat desk book is not a flat account/)
  })

  it('cannot be dismissed: it renders no button and no dismiss control', () => {
    render(<AccountExposureBanner exposure={live} />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(document.body.textContent).not.toMatch(/dismiss|hide|close/i)
  })

  it('says plainly when nothing is held outside the desk book', () => {
    render(<AccountExposureBanner exposure={exposure()} />)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('status').textContent).toMatch(
      /nothing held outside the desk book/,
    )
  })

  it('never renders an unreadable book as no exposure', () => {
    render(
      <AccountExposureBanner
        exposure={exposure({
          countable: false,
          max_loss_usd: null,
          problems: ['legacy book putspread-20260922/book.json unreadable (ValueError)'],
          outside_desk_book: { ...emptySlice, structures: 1 },
        })}
      />,
    )
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toMatch(/Account exposure unknown/)
    expect(text).toMatch(/not countable/)
    expect(text).not.toMatch(/nothing held outside/)
  })

  it('renders nothing when the backend serves no account block', () => {
    const { container } = render(<AccountExposureBanner exposure={undefined} />)
    expect(container.textContent).toBe('')
  })
})
