// A CSS media query as React state (re-renders when the viewport crosses it).
// Without matchMedia (old engines, some test setups) the query reads false.

import { useCallback, useSyncExternalStore } from 'react'

function list(query: string): MediaQueryList | null {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' ? window.matchMedia(query) : null
}

export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const mql = list(query)
      if (!mql) return () => undefined
      if (typeof mql.addEventListener === 'function') {
        mql.addEventListener('change', onChange)
        return () => mql.removeEventListener('change', onChange)
      }
      mql.addListener(onChange) // Safari < 14
      return () => mql.removeListener(onChange)
    },
    [query],
  )
  const read = useCallback(() => list(query)?.matches ?? false, [query])
  return useSyncExternalStore(subscribe, read, () => false)
}
