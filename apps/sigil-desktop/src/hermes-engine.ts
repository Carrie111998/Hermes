export type HermesEngineStatus = 'connected' | 'degraded' | 'disconnected'

export type HermesAnalysisSource = 'hermes' | 'local'

export type HermesAnalysisKind =
  | 'general-analysis'
  | 'proposal-explanation'
  | 'proposal-analysis'
  | 'portfolio-summary'
  | 'risk-evaluation'
  | 'anomaly-detection'
  | 'evidence-explanation'
  | 'operator-answer'
  | 'system-status'

export interface HermesEvidenceReference {
  readonly id: string
  readonly label?: string
  readonly source?: string
}

export interface HermesAnalysisRequest {
  readonly prompt: string
  readonly evidenceReferences: readonly HermesEvidenceReference[]
}

export interface HermesProposalContext {
  readonly proposalId: string
  readonly symbol: string
  readonly side: 'BUY' | 'SELL'
  readonly estimatedNotional: number
  readonly strategy: string
  readonly evidenceReferences: readonly HermesEvidenceReference[]
}

export interface HermesPortfolioPosition {
  readonly symbol: string
  readonly quantity: number
  readonly marketValue: number
}

export interface HermesPortfolioContext {
  readonly positions: readonly HermesPortfolioPosition[]
  readonly cashAvailable: number
  readonly firstLaunchCap: 25
}

export interface HermesRiskContext {
  readonly proposal: HermesProposalContext
  readonly firstLaunchCap: 25
  readonly brokerConnected: boolean
  readonly executionMode: 'paper' | 'simulated'
}

export interface HermesAnomalyContext {
  readonly observations: readonly string[]
  readonly evidenceReferences: readonly HermesEvidenceReference[]
}

export interface HermesOperatorQuestion {
  readonly question: string
  readonly evidenceReferences: readonly HermesEvidenceReference[]
}

export interface HermesAnalysisResult {
  readonly kind: HermesAnalysisKind
  readonly explanation: string
  readonly summary: string
  readonly modelRoute: string
  readonly source: HermesAnalysisSource
  readonly confidence: number
  readonly evidenceReferences: readonly HermesEvidenceReference[]
  readonly generatedAt: string
  readonly executionAuthorized: false
  readonly brokerSubmissionAvailable: false
}

export interface HermesSystemStatus {
  readonly kind: 'system-status'
  readonly status: HermesEngineStatus
  readonly source: HermesAnalysisSource
  readonly modelRoute: string
  readonly message: string
  readonly generatedAt: string
  readonly executionAuthorized: false
  readonly brokerSubmissionAvailable: false
}

interface LocalDesktopBridgeError {
  readonly ok: false
  readonly error: string
  readonly message: string
}

interface LocalDesktopBackendStatus {
  readonly bridge_version: string
  readonly status: string
  readonly mode: string
  readonly environment: string
  readonly simulation: boolean
  readonly execution_authorized: boolean
  readonly broker_submission_available: boolean
  readonly supported_commands: readonly string[]
}

interface LocalDesktopProposalExplanation {
  readonly kind: 'proposal-explanation'
  readonly summary: string
  readonly explanation: string
  readonly model_route: string
  readonly source: 'local'
  readonly confidence: number
  readonly evidence_references: readonly HermesEvidenceReference[]
  readonly generated_at: string
  readonly execution_authorized: false
  readonly broker_submission_available: false
}

type LocalDesktopBridgeResponse<T> =
  | {
      readonly ok: true
      readonly result: T
    }
  | LocalDesktopBridgeError

interface LocalDesktopBridge {
  getBackendStatus(): Promise<LocalDesktopBridgeResponse<LocalDesktopBackendStatus>>

  explainProposal(payload: {
    readonly proposal_id: string
    readonly symbol: string
    readonly side: 'BUY' | 'SELL'
    readonly estimated_notional: number
    readonly strategy: string
    readonly evidence_references: readonly HermesEvidenceReference[]
  }): Promise<LocalDesktopBridgeResponse<LocalDesktopProposalExplanation>>
}

function localDesktopBridge(): LocalDesktopBridge | undefined {
  return (
    window as typeof window & {
      readonly sigilDesktop?: LocalDesktopBridge
    }
  ).sigilDesktop
}

export interface SigilHermesEngine {
  readonly status: HermesEngineStatus

  analyze(request: HermesAnalysisRequest): Promise<HermesAnalysisResult>

  explain(evidenceReference: string): Promise<HermesAnalysisResult>

  analyzeProposal(context: HermesProposalContext): Promise<HermesAnalysisResult>

  explainProposal(context: HermesProposalContext): Promise<HermesAnalysisResult>

  summarizePortfolio(context: HermesPortfolioContext): Promise<HermesAnalysisResult>

  evaluateRisk(context: HermesRiskContext): Promise<HermesAnalysisResult>

  detectAnomalies(context: HermesAnomalyContext): Promise<HermesAnalysisResult>

  answerOperatorQuestion(context: HermesOperatorQuestion): Promise<HermesAnalysisResult>

  getSystemStatus(): Promise<HermesSystemStatus>
}

function now(): string {
  return new Date().toISOString()
}

function localResult(
  kind: HermesAnalysisKind,
  summary: string,
  explanation: string,
  evidenceReferences: readonly HermesEvidenceReference[] = []
): HermesAnalysisResult {
  return {
    kind,
    summary,
    explanation,
    modelRoute: 'local-disconnected',
    source: 'local',
    confidence: 1,
    evidenceReferences,
    generatedAt: now(),
    executionAuthorized: false,
    brokerSubmissionAvailable: false
  }
}

export class DisconnectedHermesEngine implements SigilHermesEngine {
  readonly status: HermesEngineStatus = 'disconnected'

  async analyze(request: HermesAnalysisRequest): Promise<HermesAnalysisResult> {
    return localResult(
      'general-analysis',
      'Local evidence remains available.',
      `Hermes analysis is unavailable. Sigil remains operational using verified local evidence for: ${request.prompt}`,
      request.evidenceReferences
    )
  }

  async explain(evidenceReference: string): Promise<HermesAnalysisResult> {
    const evidenceReferences: readonly HermesEvidenceReference[] = [
      {
        id: evidenceReference,
        label: 'Local evidence'
      }
    ]

    return localResult(
      'evidence-explanation',
      'Evidence is stored locally.',
      'This evidence is available locally. No external model or broker service was contacted.',
      evidenceReferences
    )
  }

  async analyzeProposal(context: HermesProposalContext): Promise<HermesAnalysisResult> {
    return localResult(
      'proposal-analysis',
      `${context.symbol} ${context.side} proposal remains analysis-only.`,
      `Proposal ${context.proposalId} was generated by ${context.strategy}. Its estimated notional is $${context.estimatedNotional.toFixed(
        2
      )}. No execution authorization has been granted.`,
      context.evidenceReferences
    )
  }

  async explainProposal(context: HermesProposalContext): Promise<HermesAnalysisResult> {
    return localResult(
      'proposal-explanation',
      `Proposal ${context.proposalId} is governed and simulated.`,
      `${context.symbol} is presented as a ${context.side} proposal from ${context.strategy}. The proposal may be reviewed or simulated, but it cannot be submitted to a broker.`,
      context.evidenceReferences
    )
  }

  async summarizePortfolio(context: HermesPortfolioContext): Promise<HermesAnalysisResult> {
    const totalMarketValue = context.positions.reduce((total, position) => total + position.marketValue, 0)

    return localResult(
      'portfolio-summary',
      `${context.positions.length} positions with $${totalMarketValue.toFixed(2)} in modeled market value.`,
      `The local portfolio view includes $${context.cashAvailable.toFixed(
        2
      )} cash and preserves the fixed $${context.firstLaunchCap} first-launch cap. This summary does not authorize trading.`
    )
  }

  async evaluateRisk(context: HermesRiskContext): Promise<HermesAnalysisResult> {
    const withinCap = context.proposal.estimatedNotional <= context.firstLaunchCap

    return localResult(
      'risk-evaluation',
      withinCap ? 'Proposal is within the fixed first-launch cap.' : 'Proposal exceeds the fixed first-launch cap.',
      `The proposal notional is $${context.proposal.estimatedNotional.toFixed(
        2
      )} against a fixed $${context.firstLaunchCap} cap. Execution mode is ${
        context.executionMode
      }, broker connection is ${
        context.brokerConnected ? 'reported connected' : 'disconnected'
      }, and broker submission remains unavailable.`,
      context.proposal.evidenceReferences
    )
  }

  async detectAnomalies(context: HermesAnomalyContext): Promise<HermesAnalysisResult> {
    const anomalyCount = context.observations.length

    return localResult(
      'anomaly-detection',
      `${anomalyCount} local observation${anomalyCount === 1 ? '' : 's'} reviewed.`,
      anomalyCount === 0
        ? 'No anomaly observations were supplied for local review.'
        : `Local review received: ${context.observations.join('; ')}. No external service was contacted.`,
      context.evidenceReferences
    )
  }

  async answerOperatorQuestion(context: HermesOperatorQuestion): Promise<HermesAnalysisResult> {
    return localResult(
      'operator-answer',
      'Hermes is disconnected; a local safety answer was returned.',
      `Operator question received: ${context.question}. Sigil can explain local evidence, governance state, and simulated outcomes, but it cannot authorize or submit an order.`,
      context.evidenceReferences
    )
  }

  async getSystemStatus(): Promise<HermesSystemStatus> {
    return {
      kind: 'system-status',
      status: this.status,
      source: 'local',
      modelRoute: 'local-disconnected',
      message: 'Hermes is disconnected. Sigil remains operational in local paper and simulated mode.',
      generatedAt: now(),
      executionAuthorized: false,
      brokerSubmissionAvailable: false
    }
  }
}

/**
 * Reads governed local status through Electron's narrow preload bridge.
 *
 * All analytical methods currently retain the deterministic local fallback.
 * Only system status crosses the IPC boundary in this phase.
 */
export class LocalHermesEngine extends DisconnectedHermesEngine {
  override readonly status: HermesEngineStatus = 'connected'

  override async explainProposal(context: HermesProposalContext): Promise<HermesAnalysisResult> {
    const bridge = localDesktopBridge()

    if (!bridge) {
      return super.explainProposal(context)
    }

    try {
      const response = await bridge.explainProposal({
        proposal_id: context.proposalId,
        symbol: context.symbol,
        side: context.side,
        estimated_notional: context.estimatedNotional,
        strategy: context.strategy,
        evidence_references: context.evidenceReferences.map(reference => ({
          id: reference.id,
          ...(reference.label ? { label: reference.label } : {}),
          ...(reference.source ? { source: reference.source } : {})
        }))
      })

      if (!response.ok) {
        throw new Error(response.message)
      }

      const result = response.result

      return {
        kind: result.kind,
        summary: result.summary,
        explanation: result.explanation,
        modelRoute: result.model_route,
        source: result.source,
        confidence: result.confidence,
        evidenceReferences: result.evidence_references,
        generatedAt: result.generated_at,
        executionAuthorized: false,
        brokerSubmissionAvailable: false
      }
    } catch {
      return super.explainProposal(context)
    }
  }

  override async getSystemStatus(): Promise<HermesSystemStatus> {
    const bridge = localDesktopBridge()

    if (!bridge) {
      return super.getSystemStatus()
    }

    try {
      const response = await bridge.getBackendStatus()

      if (!response.ok) {
        return {
          kind: 'system-status',
          status: 'degraded',
          source: 'local',
          modelRoute: 'local-backend-unavailable',
          message: response.message,
          generatedAt: now(),
          executionAuthorized: false,
          brokerSubmissionAvailable: false
        }
      }

      const backend = response.result

      return {
        kind: 'system-status',
        status: backend.status === 'ok' ? 'connected' : 'degraded',
        source: 'local',
        modelRoute: `python-bridge-v${backend.bridge_version}`,
        message:
          `Sigil local backend is verified in ${backend.mode} mode. ` +
          `Environment: ${backend.environment}. ` +
          'Execution authorization and broker submission remain disabled.',
        generatedAt: now(),
        executionAuthorized: false,
        brokerSubmissionAvailable: false
      }
    } catch (reason) {
      return {
        kind: 'system-status',
        status: 'degraded',
        source: 'local',
        modelRoute: 'local-backend-error',
        message: reason instanceof Error ? reason.message : 'The local Sigil backend status could not be verified.',
        generatedAt: now(),
        executionAuthorized: false,
        brokerSubmissionAvailable: false
      }
    }
  }
}
