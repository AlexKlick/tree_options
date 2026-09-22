import { createContext, useContext, type ReactNode } from 'react'
import { useLocalStorage } from './hooks/useLocalStorage'

export type DensityMode = 'operator' | 'simple'

interface DensityCtx {
  mode: DensityMode
  setMode: (m: DensityMode) => void
}

const Ctx = createContext<DensityCtx>({ mode: 'operator', setMode: () => undefined })

export function DensityProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useLocalStorage<DensityMode>('trex.density', 'operator')
  return <Ctx.Provider value={{ mode, setMode }}>{children}</Ctx.Provider>
}

export const useDensity = (): DensityCtx => useContext(Ctx)
