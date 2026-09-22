import { useDensity, type DensityMode } from '../density'

const OPTIONS: { id: DensityMode; label: string }[] = [
  { id: 'operator', label: 'Operator' },
  { id: 'simple', label: 'Simple' },
]

export function DensityToggle() {
  const { mode, setMode } = useDensity()
  return (
    <div className="density-toggle" role="group" aria-label="Density">
      {OPTIONS.map((opt) => (
        <button
          key={opt.id}
          type="button"
          aria-pressed={mode === opt.id}
          onClick={() => setMode(opt.id)}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}
