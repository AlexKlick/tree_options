import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import type { ExitMachineStatus } from '../lib/types'
import { getExitMachine } from '../lib/api'
import { ExitMachineBanner } from './ExitMachineBanner'

vi.mock('../lib/api', () => ({ getExitMachine: vi.fn() }))
const mocked = vi.mocked(getExitMachine)

const now = Date.now() / 1000
const base: ExitMachineStatus = {
  status: 'monitor_down',
  since: now - 15 * 60,
  detail: 'putspread-20260922: no heartbeat for 15m (unit failed)',
  checked_at: now - 10,
  books: [{ plan: 'putspread-20260922', status: 'monitor_down', detail: 'x', heartbeat_age: 900 }],
  age_seconds: 10,
  watch_stale: false,
}

// The monitor can be dead, or alive with every tick failing, while the
// gateway is fine: the book then has no touch or time-stop exits.
describe('ExitMachineBanner', () => {
  afterEach(() => vi.clearAllMocks())

  it('says the exit machine is down, since when, and what that means', async () => {
    mocked.mockResolvedValue(base)
    render(<ExitMachineBanner />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toMatch(/Exit machine is not running/)
    expect(text).toMatch(/15m/)
    expect(text).toMatch(/no touch or time-stop exits/)
    expect(text).toMatch(/putspread-20260922/)
  })

  it('says when it runs but its checks fail', async () => {
    mocked.mockResolvedValue({ ...base, status: 'monitor_failing' })
    render(<ExitMachineBanner />)
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toMatch(/checks are failing/),
    )
  })

  it('defers to the gateway banner while it waits for the gateway', async () => {
    mocked.mockResolvedValue({ ...base, status: 'waiting_for_gateway' })
    render(<ExitMachineBanner />)
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toMatch(/waiting for the IB Gateway/),
    )
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('renders nothing when guarded or with no open positions', async () => {
    for (const status of ['ok', 'idle'] as const) {
      mocked.mockResolvedValue({ ...base, status })
      const { container, unmount } = render(<ExitMachineBanner />)
      await waitFor(() => expect(mocked).toHaveBeenCalled())
      expect(container.textContent).toBe('')
      unmount()
    }
  })

  it('never lets a silent watchdog read as healthy, keeping an old alarm as last-known', async () => {
    mocked.mockResolvedValue({ ...base, watch_stale: true, age_seconds: 1800 })
    render(<ExitMachineBanner />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toMatch(/last reported 30m ago/)
    expect(text).toMatch(/last known: Exit machine is not running/)
  })

  it('says health is unknown when its status cannot be read', async () => {
    mocked.mockRejectedValue(new Error('HTTP 500'))
    render(<ExitMachineBanner />)
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toMatch(/exit machine health unknown/),
    )
  })
})

// Codex P2 (2026-09-23): a cached "ok" whose later polls never settle kept
// rendering nothing; so did a fresh "unknown".
describe('ExitMachineBanner freshness', () => {
  afterEach(() => vi.clearAllMocks())

  it('ages a cached verdict locally, so a hung poll cannot keep it healthy', async () => {
    mocked.mockResolvedValue({ ...base, status: 'ok', checked_at: now - 900, age_seconds: 5 })
    render(<ExitMachineBanner />)
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toMatch(/exit machine health unknown/),
    )
  })

  it('never renders an unrecognized status as healthy', async () => {
    mocked.mockResolvedValue({ ...base, status: 'unknown' })
    render(<ExitMachineBanner />)
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toMatch(/exit machine health unknown/),
    )
  })
})
