// Horizontal-scroll wrapper for wide tables. When the table is wider than
// its box (phones), the cut edge fades and a hint says more columns exist;
// a clipped "Ma" / "+$2" otherwise reads as a real (wrong) value (M8 flash
// review). Inert where the table fits.

import { useLayoutEffect, useRef, useState, type ReactNode } from 'react'

export function TableScroll({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [more, setMore] = useState(false)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const update = (): void =>
      setMore(el.scrollWidth - el.clientWidth - el.scrollLeft > 2)
    update()
    el.addEventListener('scroll', update, { passive: true })
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(update) : null
    ro?.observe(el)
    if (el.firstElementChild) ro?.observe(el.firstElementChild)
    return () => {
      el.removeEventListener('scroll', update)
      ro?.disconnect()
    }
  }, [])

  return (
    <>
      {more && (
        <p className="scroll-hint muted" aria-hidden="true">
          scroll for more columns →
        </p>
      )}
      <div ref={ref} className={`table-scroll${more ? ' has-more' : ''}`}>
        {children}
      </div>
    </>
  )
}
