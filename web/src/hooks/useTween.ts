import { useEffect, useRef, useState } from 'react'

/** Ease-out tween toward the target number (instant under reduced motion). */
export function useTween(target: number, durationMs = 250): number {
  const [value, setValue] = useState(target)
  const valueRef = useRef(target)

  useEffect(() => {
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduced || Math.abs(target - valueRef.current) < 0.005) {
      valueRef.current = target
      setValue(target)
      return
    }
    const from = valueRef.current
    const start = performance.now()
    let raf = 0
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / durationMs)
      const eased = 1 - (1 - t) ** 3
      const next = from + (target - from) * eased
      valueRef.current = next
      setValue(next)
      if (t < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target, durationMs])

  return value
}
