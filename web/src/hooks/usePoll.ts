import { useCallback, useEffect, useRef, useState } from 'react'

export interface PollState<T> {
  data: T | null
  error: string | null
  lastSuccessAt: number | null
  isStale: boolean
  refresh: () => void
}

/**
 * Poll a fetcher on an interval. Pauses while the tab is hidden and
 * refetches immediately on regain; a failed fetch keeps the last data
 * and sets `error` (the UI never unmounts on a blip).
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

  const run = useCallback(async () => {
    try {
      const next = await fetcherRef.current()
      if (!aliveRef.current) return
      setData(next)
      setError(null)
      setLastSuccessAt(Date.now())
    } catch (err) {
      if (!aliveRef.current) return
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    aliveRef.current = true
    void run()
    const onVisibility = () => {
      if (!document.hidden) void run()
    }
    document.addEventListener('visibilitychange', onVisibility)
    const timer = window.setInterval(() => {
      if (!document.hidden) void run()
    }, intervalMs)
    const clock = window.setInterval(() => tick((n) => n + 1), 5_000)
    return () => {
      aliveRef.current = false
      document.removeEventListener('visibilitychange', onVisibility)
      window.clearInterval(timer)
      window.clearInterval(clock)
    }
  }, [run, intervalMs])

  const isStale =
    lastSuccessAt === null ? error !== null : Date.now() - lastSuccessAt > staleAfterMs

  return { data, error, lastSuccessAt, isStale, refresh: () => void run() }
}
