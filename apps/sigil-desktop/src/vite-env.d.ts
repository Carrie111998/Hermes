/// <reference types="vite/client" />

type SigilBackendStatus = {
  bridge_version: string
  status: string
  mode: string
  environment: string
  simulation: boolean
  execution_authorized: boolean
  broker_submission_available: boolean
  supported_commands: string[]
}

type SigilBackendProposalExplanation = {
  kind: 'proposal-explanation'
  summary: string
  explanation: string
  model_route: string
  source: 'local'
  confidence: number
  evidence_references: Array<{
    id: string
    label?: string
    source?: string
  }>
  generated_at: string
  execution_authorized: false
  broker_submission_available: false
}

type SigilBackendResponse<T> =
  | {
      ok: true
      result: T
    }
  | {
      ok: false
      error: string
      message: string
    }

type SigilProposalExplanationPayload = {
  proposal_id: string
  symbol: string
  side: 'BUY' | 'SELL'
  estimated_notional: number
  strategy: string
  evidence_references: Array<{
    id: string
    label?: string
    source?: string
  }>
}

interface SigilDesktopApi {
  productName: string
  persistenceNamespace: string
  brokerSubmissionAvailable: false
  getBackendStatus: () => Promise<SigilBackendResponse<SigilBackendStatus>>
  explainProposal: (
    payload: SigilProposalExplanationPayload
  ) => Promise<SigilBackendResponse<SigilBackendProposalExplanation>>
}

interface Window {
  sigilDesktop?: SigilDesktopApi
}
