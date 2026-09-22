import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePoll } from './usePoll'

function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', {
    value: hidden,
    configurable: true,
  })
  Object.defineProperty(document, 'visibilityState', {
    value: hidden ? 'hidden' : 'visible',
    configurable: true,
  })
}

describe('usePoll', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setHidden(false)
  })
  afterEach(() => {
    vi.useRealTimers()
    setHidden(false)
  })

  it('fetches on mount and on the interval', async () => {
    const fetcher = vi.fn().mockResolvedValue({ n: 1 })
    const { result } = renderHook(() => usePoll(fetcher, 1_000))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(result.current.data).toEqual({ n: 1 })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('pauses while the tab is hidden and refetches on regain', async () => {
    const fetcher = vi.fn().mockResolvedValue({ n: 1 })
    renderHook(() => usePoll(fetcher, 1_000))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(fetcher).toHaveBeenCalledTimes(1)

    setHidden(true)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(fetcher).toHaveBeenCalledTimes(1) // no ticks while hidden

    setHidden(false)
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    expect(fetcher).toHaveBeenCalledTimes(2) // immediate refetch on regain
  })

  it('keeps the last data when a fetch fails', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce({ n: 1 })
      .mockRejectedValueOnce(new Error('boom'))
    const { result } = renderHook(() => usePoll(fetcher, 1_000))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.data).toEqual({ n: 1 })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(result.current.data).toEqual({ n: 1 }) // kept
    expect(result.current.error).toBe('boom')
    expect(result.current.isStale).toBe(false) // last success is recent
  })
})
