import { useCallback, useEffect, useRef, useState } from 'react'

export interface PollState<T> {
  data: T | null
  error: string | null
  lastSuccessAt: number | null
  isStale: boolean
  refresh: () => void
}

/**
 * Poll a fetcher on an interval — or run it once when polling is
 * disabled.
 *
 * `intervalMs <= 0` means POLLING DISABLED: the fetcher runs once on
 * mount and again only through an explicit `refresh()`. This is the
 * evidence drawer's mode; passing 0 must never become a zero-ms
 * interval.
 *
 * In interval mode the hook pauses while the tab is hidden and
 * refetches immediately on regain; a failed fetch keeps the last data
 * and sets `error` (the UI never unmounts on a blip).
 *
 * Overlapping completions can never reorder: every fetch carries a
 * generation number and only the latest generation may commit state, so
 * a slower, older response can never overwrite a newer one. Interval
 * ticks are skipped while a fetch is in flight (non-overlapping
 * scheduling); a forced `refresh()` always starts a new generation and
 * supersedes whatever was in flight.
 */
export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs = 15_000,
  staleAfterMs = 45_000,
): PollState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lastSuccessAt, setLastSuccessAt] = useState<number | null>(null)
  const [, tick] = useState(0) // slow clock so "Ns ago" badges stay live
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher
  const aliveRef = useRef(true)
  const latestGenRef = useRef(0)
  const inFlightRef = useRef(false)

  const run = useCallback(async (force: boolean) => {
    if (inFlightRef.current && !force) return // tick while in flight: skip
    const gen = ++latestGenRef.current
    inFlightRef.current = true
    try {
      const next = await fetcherRef.current()
      if (!aliveRef.current || gen !== latestGenRef.current) return // stale loses
      setData(next)
      setError(null)
      setLastSuccessAt(Date.now())
    } catch (err) {
      if (!aliveRef.current || gen !== latestGenRef.current) return
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      if (gen === latestGenRef.current) inFlightRef.current = false
    }
  }, [])

  useEffect(() => {
    aliveRef.current = true
    void run(false)
    const cleanupFns: Array<() => void> = []
    if (intervalMs > 0) {
      const onVisibility = () => {
        if (!document.hidden) void run(false)
      }
      document.addEventListener('visibilitychange', onVisibility)
      const timer = window.setInterval(() => {
        if (!document.hidden) void run(false)
      }, intervalMs)
      const clock = window.setInterval(() => tick((n) => n + 1), 5_000)
      cleanupFns.push(
        () => document.removeEventListener('visibilitychange', onVisibility),
        () => window.clearInterval(timer),
        () => window.clearInterval(clock),
      )
    }
    return () => {
      aliveRef.current = false
      for (const fn of cleanupFns) fn()
    }
  }, [run, intervalMs])

  const isStale =
    lastSuccessAt === null ? error !== null : Date.now() - lastSuccessAt > staleAfterMs

  return { data, error, lastSuccessAt, isStale, refresh: () => void run(true) }
}
