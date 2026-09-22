import type { ReactNode } from 'react'

export type PillVariant = 'armed' | 'disarmed' | 'empty'

export function Pill({
  variant,
  children,
}: {
  variant: PillVariant
  children: ReactNode
}) {
  return <span className={`pill pill-${variant}`}>{children}</span>
}
