import { useEffect, useState } from 'react'
import { parseHash, type Route } from './lib/router'
import { DensityProvider } from './density'
import { DiscoverPage } from './components/DiscoverPage'
import { PerformancePage } from './components/PerformancePage'
import { PlanDetail } from './components/PlanDetail'
import { PlanList } from './components/PlanList'

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseHash(location.hash))
  useEffect(() => {
    const onHash = () => setRoute(parseHash(location.hash))
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  return (
    <DensityProvider>
      {route.view === 'plan' ? (
        <PlanDetail key={route.id} id={route.id} />
      ) : route.view === 'discover' ? (
        <DiscoverPage />
      ) : route.view === 'stats' ? (
        <PerformancePage />
      ) : (
        <PlanList />
      )}
    </DensityProvider>
  )
}
