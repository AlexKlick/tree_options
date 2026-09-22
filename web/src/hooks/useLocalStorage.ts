import { useCallback, useState } from 'react'

/** String-valued localStorage with a safe fallback (private mode etc.). */
export function useLocalStorage<T extends string>(
  key: string,
  initial: T,
): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key)
      return raw === null ? initial : (raw as T)
    } catch {
      return initial
    }
  })
  const set = useCallback(
    (v: T) => {
      setValue(v)
      try {
        window.localStorage.setItem(key, v)
      } catch {
        /* storage unavailable: keep in-memory value */
      }
    },
    [key],
  )
  return [value, set]
}
