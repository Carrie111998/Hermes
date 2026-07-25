export interface HermesAnalysisRequest {
  readonly evidenceReferences: readonly string[]
  readonly prompt: string
}

export interface HermesAnalysisResult {
  readonly explanation: string
  readonly modelRoute: string
  readonly source: 'hermes' | 'local'
}

export interface SigilHermesEngine {
  analyze(request: HermesAnalysisRequest): Promise<HermesAnalysisResult>
  explain(evidenceReference: string): Promise<HermesAnalysisResult>
  readonly status: 'connected' | 'disconnected'
}

export class DisconnectedHermesEngine implements SigilHermesEngine {
  readonly status = 'disconnected' as const

  async analyze(_request: HermesAnalysisRequest): Promise<HermesAnalysisResult> {
    return {
      explanation: 'Hermes analysis is unavailable. Sigil remains operational with verified local evidence.',
      modelRoute: 'local-disconnected',
      source: 'local'
    }
  }

  async explain(_evidenceReference: string): Promise<HermesAnalysisResult> {
    return {
      explanation: 'This evidence is available locally. No external model was contacted.',
      modelRoute: 'local-disconnected',
      source: 'local'
    }
  }
}
