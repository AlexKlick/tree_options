import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import type { GatewayStatus } from '../lib/types'
import { getGateway } from '../lib/api'
import { GatewayBanner } from './GatewayBanner'

vi.mock('../lib/api', () => ({ getGateway: vi.fn() }))
const mocked = vi.mocked(getGateway)

const now = Date.now() / 1000
const base: GatewayStatus = {
  status: 'needs_login',
  since: now - (14 * 3600 + 33 * 60),
  detail: 'login screen open, not logged in',
  checked_at: now - 20,
  login_url: 'https://example.invalid/vnc.html?path=vnc',
  restarts_left: 3,
  next_restart_at: now + 3600,
  ibc_phase: 'login_dialog',
  api_ok: false,
  vnc_running: true,
  age_seconds: 20,
  watch_stale: false,
  last_restart_at: now - 3600,
}

// The 2026-09-22/23 incident: the gateway sat logged out for ~14h while
// every page looked normal apart from quietly aging marks.
describe('GatewayBanner', () => {
  afterEach(() => vi.clearAllMocks())

  it('says the gateway needs a login, since when, what it blocks, and links the login screen', async () => {
    mocked.mockResolvedValue(base)
    render(<GatewayBanner />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toMatch(/IB Gateway needs a login/)
    expect(alert.textContent).toMatch(/14h 33m/)
    expect(alert.textContent).toMatch(/can't mark or exit/)
    expect(alert.textContent).toMatch(/3 auto-retries left today/)
    const link = screen.getByRole('link', { name: /open the login screen/i })
    expect(link.getAttribute('href')).toBe(base.login_url)
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.className).toContain('tap-link')
  })

  it('asks for the 2FA approval', async () => {
    mocked.mockResolvedValue({ ...base, status: 'needs_2fa' })
    render(<GatewayBanner />)
    await waitFor(() => expect(screen.getByRole('alert').textContent).toMatch(/2FA/))
  })

  it('says when auto-retry is used up', async () => {
    mocked.mockResolvedValue({ ...base, restarts_left: 0 })
    render(<GatewayBanner />)
    await waitFor(() =>
      expect(screen.getByRole('alert').textContent).toMatch(/auto-retry used up/),
    )
  })

  it('renders nothing while the gateway is healthy and the watchdog is reporting', async () => {
    mocked.mockResolvedValue({ ...base, status: 'ok' })
    const { container } = render(<GatewayBanner />)
    await waitFor(() => expect(mocked).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })

  it('never lets a silent watchdog read as healthy', async () => {
    mocked.mockResolvedValue({ ...base, status: 'ok', watch_stale: true, age_seconds: 900 })
    render(<GatewayBanner />)
    await waitFor(() => expect(screen.getByText(/watchdog last reported 15m ago/)).toBeTruthy())
  })

  // Codex P2 (2026-09-23): an /api/gateway failure rendered nothing while the
  // header's independent poll still said "API connected".
  it('says gateway health is unknown when its status cannot be read', async () => {
    mocked.mockRejectedValue(new Error('HTTP 500'))
    render(<GatewayBanner />)
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toMatch(/gateway health unknown/),
    )
  })

  // Codex P2: a stale needs_login kept issuing current instructions.
  it('shows a stale alarm as last-known, not as current', async () => {
    mocked.mockResolvedValue({ ...base, watch_stale: true, age_seconds: 1800 })
    render(<GatewayBanner />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    const text = screen.getByRole('alert').textContent ?? ''
    expect(text).toMatch(/watchdog last reported 30m ago/)
    expect(text).toMatch(/last known: IB Gateway needs a login/)
    expect(text).not.toMatch(/auto-retr/) // a stale retry budget is not current
  })

  it('shows a short API miss as a recheck, not an alarm', async () => {
    mocked.mockResolvedValue({
      ...base,
      status: 'checking',
      detail: 'API not answering (TimeoutError); rechecking',
    })
    render(<GatewayBanner />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/rechecking/))
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
