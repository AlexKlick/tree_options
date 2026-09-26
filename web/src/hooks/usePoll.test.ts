import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { usePoll } from './usePoll'

// Deterministic deferreds so tests control completion ORDER — the
// property under test is "an older completion can never overwrite a
// newer one", which real timers never exercise reliably.
function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

describe('usePoll', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('intervalMs=0 disables polling: one request, no interval installed', async () => {
    const setInt = vi.spyOn(window, 'setInterval')
    const fetcher = vi.fn(() => Promise.resolve('x'))
    renderHook(() => usePoll(fetcher, 0))
    await act(async () => {
      vi.advanceTimersByTime(120_000)
      await Promise.resolve()
    })
    expect(fetcher).toHaveBeenCalledTimes(1)
    // Zero must be treated as "disabled", never as a 0ms interval —
    // not even the 5s staleness clock belongs in one-shot mode.
    expect(setInt).not.toHaveBeenCalled()
  })

  it('a stale completion cannot overwrite a newer response', async () => {
    const first = deferred<string>()
    const older = deferred<string>()
    const newer = deferred<string>()
    const pending = [first, older, newer]
    const fetcher = vi.fn(() => pending.shift()!.promise)
    const { result } = renderHook(() => usePoll(fetcher, 0))

    await act(async () => { first.resolve('initial'); await Promise.resolve() })
    expect(result.current.data).toBe('initial')

    // Two forced refreshes: the second supersedes the first while both
    // are in flight. Resolve the NEWER first, the OLDER last — the
    // audit's probe ordering.
    act(() => { result.current.refresh() })
    act(() => { result.current.refresh() })
    await act(async () => { newer.resolve('newer'); await Promise.resolve() })
    expect(result.current.data).toBe('newer')
    await act(async () => { older.resolve('older'); await Promise.resolve() })
    expect(result.current.data).toBe('newer')
  })

  it('interval mode keeps polling (existing behavior control)', async () => {
    const mount = deferred<number>()
    let pastMount = false
    const fetcher = vi.fn(() => (pastMount ? Promise.resolve(2) : mount.promise))
    const { result } = renderHook(() => usePoll(fetcher, 1_000))
    // settle the mount fetch BEFORE any tick, so ticks find no in-flight run
    await act(async () => {
      mount.resolve(1)
      await Promise.resolve()
      await Promise.resolve()
    })
    pastMount = true
    expect(fetcher).toHaveBeenCalledTimes(1)
    await act(async () => {
      vi.advanceTimersByTime(1_000)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(fetcher).toHaveBeenCalledTimes(2)
    expect(result.current.data).toBe(2)
    // One interval at a time (a batch advance would fire all callbacks
    // back-to-back with no microtask drain between them, and the
    // non-overlap scheduler correctly skips those).
    for (let i = 0; i < 3; i += 1) {
      await act(async () => {
        vi.advanceTimersByTime(1_000)
        await Promise.resolve()
        await Promise.resolve()
      })
    }
    expect(fetcher).toHaveBeenCalledTimes(5)
  })

  it('interval ticks are skipped while a fetch is in flight', async () => {
    const mount = deferred<number>()
    const held = deferred<number>()
    let useHeld = false
    const fetcher = vi.fn(() => (useHeld ? held.promise : mount.promise))
    const { result } = renderHook(() => usePoll(fetcher, 1_000))
    await act(async () => {
      mount.resolve(0)
      await Promise.resolve()
      await Promise.resolve()
    })
    useHeld = true
    await act(async () => {
      vi.advanceTimersByTime(1_000) // first tick: starts the held fetch
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(fetcher).toHaveBeenCalledTimes(2) // mount + first tick (held)
    await act(async () => {
      vi.advanceTimersByTime(10_000) // ticks while held: no overlap
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(fetcher).toHaveBeenCalledTimes(2)
    await act(async () => {
      held.resolve(42)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.data).toBe(42)
    await act(async () => {
      vi.advanceTimersByTime(1_000) // free again → next tick fetches
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(fetcher).toHaveBeenCalledTimes(3)
  })

  it('unmount clears timers and discards a late completion', async () => {
    const fetcher = vi.fn(() => Promise.resolve(1))
    const { unmount } = renderHook(() => usePoll(fetcher, 1_000))
    unmount()
    await act(async () => {
      vi.advanceTimersByTime(60_000)
      await Promise.resolve()
    })
    expect(fetcher).toHaveBeenCalledTimes(1) // no post-unmount polling

    const late = deferred<number>()
    const oneShot = vi.fn(() => late.promise)
    const { unmount: unmount2 } = renderHook(() => usePoll(oneShot, 0))
    unmount2()
    await act(async () => { late.resolve(5); await Promise.resolve() })
    expect(oneShot).toHaveBeenCalledTimes(1) // resolved after unmount, no throw
  })
})
