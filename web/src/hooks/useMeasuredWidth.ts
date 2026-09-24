// Measured CSS-pixel width of the referenced block (640 until the
// ResizeObserver reports): charts draw their SVG viewBox at this width so
// 12px labels render at 12px on a phone and the chart does not grow tall
// on a widescreen. Extracted verbatim from TimeSeriesChart for the symbol
// history chart; behavior is identical for every existing consumer.

import { useLayoutEffect, useState, type RefObject } from 'react'

const DEFAULT_WIDTH = 640

export function useMeasuredWidth(ref: RefObject<HTMLDivElement | null>): number {
  const [width, setWidth] = useState(DEFAULT_WIDTH)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width
      if (w && w > 0) setWidth(w)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [ref])
  return width
}
