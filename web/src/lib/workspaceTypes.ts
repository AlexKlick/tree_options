export interface ResearchDataset {
  dataset_id: string
  label: string
  data_class: 'synthetic_fixture' | 'user_supplied_unqualified'
  promotion_allowed: false
  splits?: Record<'train' | 'validation' | 'holdout', {period_count: number; decision_start: string; decision_end: string}>
  universe_count?: number
}

export interface ResearchJob {
  run_id: string
  status: string
  hypothesis: string
  dataset_id: string
  capital: string
  max_candidates: number
  generations: number
  strategy_id: string
  top_n: number | null
  reflect_glm53: boolean
  sleeve_id?: string | null
  data_class: string
  error?: string | null
  result_summary?: {
    disposition: string
    mean_net_return: string | null
    control_mean_net_return: string | null
    period_count: number | null
    fees: string | null
    evidence_kind: string
    data_class: string
    exact_external_economics: false
  }
}

export interface ResearchAssignment {
  hypothesis: string
  dataset_id: string
  capital: string
  max_candidates: number
  generations: number
  strategy_id: string
  top_n: number | null
  reflect_glm53: boolean
  sleeve_id?: string | null
}

export interface PaperAccount {
  provider?: 'snaptrade' | 'ibkr'
  equity_execution_ready?: boolean
  account_alias: string
  configured: boolean
  qualification_status: string
  assessed_at: string | null
  expires_at: string | null
  owner_held: boolean
  blockers: string[]
}

export interface PaperSleeve {
  sleeve_id: string
  label: string
  capital_usd: string
  account_alias: string | null
  reserved_usd: string
}

export interface PaperAllocation {
  plan_id: string
  total_capital_usd: string
  sleeves: PaperSleeve[]
  execution_authorized: false
  live_money: false
}

export interface PaperDeployment {
  deployment_id: string
  account_alias: string
  strategy_version: string
  research_job_id: string | null
  sleeve_id?: string | null
  intended_capital_usd: string
  max_gross_notional_usd: string
  max_orders: number
  ttl_seconds: number
  status: string
  execution_status: string
  blockers: string[]
  live_money: false
}

export interface PaperProposal {
  account_alias: string
  strategy_version: string
  research_job_id: string | null
  sleeve_id?: string | null
  intended_capital_usd: string
  max_gross_notional_usd: string
  max_orders: number
  ttl_seconds: number
  idempotency_key: string
}
