export type SigilDataState = 'disconnected' | 'empty' | 'error' | 'loading' | 'ready' | 'stale'
export type SigilTone = 'danger' | 'info' | 'muted' | 'success' | 'warning'

export interface SigilMetric {
  label: string
  value: string
  detail: string
  tone?: SigilTone
}

export interface PipelineStage {
  id: string
  label: string
  detail: string
  state: 'blocked' | 'complete' | 'pending' | 'ready' | 'simulated'
}

export interface Proposal {
  id: string
  symbol: string
  side: 'BUY' | 'SELL'
  quantity: number
  estimatedNotional: string
  strategy: string
  status: 'approved' | 'pending' | 'rejected'
  evidenceReferences: string[]
  riskResults: string[]
}

export interface ExecutionReceipt {
  id: string
  orderId: string
  symbol: string
  brokerStatus: string
  state: 'outcome-uncertain' | 'rejected' | 'simulated'
  duplicatePrevention: string
  reconciliationRequired: boolean
  timestamp: string
}

export interface AuditEvent {
  id: string
  timestamp: string
  orderId: string
  proposalId: string
  status: string
  evidenceReference: string
  summary: string
  details: Readonly<Record<string, unknown>>
}

export interface SigilSnapshot {
  dataState: SigilDataState
  lastUpdated: string
  environment: 'paper' | 'live'
  simulation: boolean
  brokerConnection: 'connected' | 'disconnected'
  maskedAccountId: string
  systemHealth: string
  cash: string
  portfolioValue: string
  activeStrategies: number
  pendingApprovals: number
  killSwitch: 'armed' | 'engaged'
  certificationStatus: string
  maximumLaunchNotional: string
  firstLaunchLimit: string
  launchState: 'armed' | 'suspended'
  stages: PipelineStage[]
  proposals: Proposal[]
  receipts: ExecutionReceipt[]
  auditEvents: AuditEvent[]
}

export type SimulatedOperatorAction =
  | { type: 'approve-proposal'; proposalId: string }
  | { type: 'reject-proposal'; proposalId: string }
  | { type: 'arm-launch' }
  | { type: 'suspend-launch' }
  | { type: 'engage-kill-switch' }

/**
 * Deliberately excludes broker submission and capital-limit mutation.
 * Step 34 can observe governed state and simulate bounded operator decisions;
 * it cannot cross the Step 33 submission boundary.
 */
export interface SigilOperatorAdapter {
  readSnapshot(): Promise<SigilSnapshot>
  applySimulatedAction(action: SimulatedOperatorAction): Promise<SigilSnapshot>
}
