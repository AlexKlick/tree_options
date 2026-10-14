export interface Experiment {
  id: string; source: string; title: string; hypothesis: string
  evidence_class: 'historical_simulation' | 'forward_shadow' | 'broker_paper' | 'live' | 'unavailable'
  status: string; source_time: string | null; freshness: string; freshness_reason: string
  metrics: {label: string; value: number | string | null; unit: string; reason: string | null}[]
  limitations: string[]; blockers: string[]; next_decision: string
  artifacts: {label: string; sha256: string}[]
  supersedes: string | null; superseded_by: string | null; advanced_link: string
}
export interface ExperimentIndex {
  published_at: string; source_release: string
  sources: {id: string; status: string; reason: string | null; count: number | null}[]
  experiments: Experiment[]
  pilot: {id: string; title: string; hypothesis: string; state: string; blockers: string[]; next_decision: string}[]
}
export async function getExperiments(): Promise<ExperimentIndex> {
  const response = await fetch('api/experiments', {headers: {Accept: 'application/json'}})
  if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Experiment inventory unavailable. Check your private session and retry.')
  const data = await response.json()
  if (!response.ok) throw new Error(`Experiment inventory unavailable (${data.reason ?? response.status}). ${data.next_step ?? 'Retry when the source is available.'}`)
  return data
}
