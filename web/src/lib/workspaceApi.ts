import { fetchJson } from './api'
import type { PaperAccount, PaperAllocation, PaperDeployment, PaperProposal, ResearchAssignment, ResearchDataset, ResearchJob } from './workspaceTypes'

export const getResearchDatasets = () => fetchJson<{datasets: ResearchDataset[]; controls_enabled?: boolean}>('api/research/quant/datasets')
export const getResearchJobs = () => fetchJson<{jobs: ResearchJob[]; controls_enabled?: boolean}>('api/research/quant/jobs')
export const getResearchJob = (id: string) => fetchJson<{job: ResearchJob; result: unknown; provenance: unknown[]}>(`api/research/quant/jobs/${encodeURIComponent(id)}`)

function post<T>(url: string, body: unknown): Promise<T> {
  return fetchJson<T>(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
}

export const assignResearch = (body: ResearchAssignment) => post<ResearchJob>('api/research/quant/jobs', body)
export const controlResearchJob = (id: string, action: 'stop' | 'resume') => post<{job: ResearchJob}>(`api/research/quant/jobs/${encodeURIComponent(id)}/${action}`, {})
export const getPaperAccounts = () => fetchJson<{accounts: PaperAccount[]; setup_required: boolean; blockers: string[]; controls_enabled?: boolean}>('api/paper/accounts')
export const getPaperDeployments = () => fetchJson<{deployments: PaperDeployment[]}>('api/paper/deployments')
export const getPaperAllocations = () => fetchJson<{plans: PaperAllocation[]}>('api/paper/allocations')
export const createPaperAllocation = (accountAlias: string | null, key: string) => post<PaperAllocation>('api/paper/allocations', {account_alias: accountAlias, idempotency_key: key})
export const bindPaperSleeve = (plan: string, sleeve: string, account: string) => post<PaperAllocation>(`api/paper/allocations/${encodeURIComponent(plan)}/sleeves/${encodeURIComponent(sleeve)}/bind`, {account_alias: account})
export const proposePaperDeployment = (body: PaperProposal) => post<PaperDeployment>('api/paper/deployments', body)
export const haltPaperDeployment = (id: string) => post<PaperDeployment>(`api/paper/deployments/${encodeURIComponent(id)}/halt`, {})
