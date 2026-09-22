import { useTween } from '../hooks/useTween'

/** A number that eases toward its target on change (formatted per frame). */
export function TweenNumber({
  value,
  format,
  className,
}: {
  value: number
  format: (v: number) => string
  className?: string
}) {
  const tweened = useTween(value)
  return <div className={className}>{format(tweened)}</div>
}
