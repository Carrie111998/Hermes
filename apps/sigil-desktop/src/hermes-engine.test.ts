import { describe, expect, it } from 'vitest'

import {
  DisconnectedHermesEngine,
  type HermesProposalContext,
  LocalHermesEngine,
  type SigilHermesEngine
} from './hermes-engine'

const proposal: HermesProposalContext = {
  proposalId: 'PRP-STEP36-0001',
  symbol: 'MSFT',
  side: 'BUY',
  estimatedNotional: 22.64,
  strategy: 'Quality momentum v2',
  evidenceReferences: [
    {
      id: 'evidence-step36-1',
      label: 'Local proposal evidence',
      source: 'sigil'
    }
  ]
}

describe('DisconnectedHermesEngine', () => {
  it('provides a typed local intelligence boundary', async () => {
    const engine: SigilHermesEngine = new DisconnectedHermesEngine()

    expect(engine.status).toBe('disconnected')

    const result = await engine.analyzeProposal(proposal)

    expect(result.kind).toBe('proposal-analysis')
    expect(result.source).toBe('local')
    expect(result.modelRoute).toBe('local-disconnected')
    expect(result.executionAuthorized).toBe(false)
    expect(result.brokerSubmissionAvailable).toBe(false)
    expect(result.evidenceReferences).toEqual(proposal.evidenceReferences)
  })

  it('evaluates the fixed $25 first-launch cap without changing it', async () => {
    const engine = new DisconnectedHermesEngine()

    const withinCap = await engine.evaluateRisk({
      proposal,
      firstLaunchCap: 25,
      brokerConnected: false,
      executionMode: 'paper'
    })

    expect(withinCap.summary).toContain('within the fixed first-launch cap')
    expect(withinCap.explanation).toContain('fixed $25 cap')

    const aboveCap = await engine.evaluateRisk({
      proposal: {
        ...proposal,
        estimatedNotional: 25.01
      },
      firstLaunchCap: 25,
      brokerConnected: false,
      executionMode: 'simulated'
    })

    expect(aboveCap.summary).toContain('exceeds the fixed first-launch cap')
    expect(aboveCap.executionAuthorized).toBe(false)
  })

  it('answers operator questions without authorizing execution', async () => {
    const engine = new DisconnectedHermesEngine()

    const result = await engine.answerOperatorQuestion({
      question: 'Why is this proposal blocked?',
      evidenceReferences: proposal.evidenceReferences
    })

    expect(result.kind).toBe('operator-answer')
    expect(result.explanation).toContain('cannot authorize or submit an order')
    expect(result.executionAuthorized).toBe(false)
  })

  it('reports disconnected local-only status', async () => {
    const engine = new DisconnectedHermesEngine()
    const status = await engine.getSystemStatus()

    expect(status.status).toBe('disconnected')
    expect(status.source).toBe('local')
    expect(status.executionAuthorized).toBe(false)
    expect(status.brokerSubmissionAvailable).toBe(false)
  })

  it('contains no broker execution capability', () => {
    const engine = new DisconnectedHermesEngine()
    const keys = Object.getOwnPropertyNames(Object.getPrototypeOf(engine)).map(key => key.toLowerCase())

    expect(keys).not.toContain('submitorder')
    expect(keys).not.toContain('executeorder')
    expect(keys).not.toContain('connectbroker')
    expect(keys).not.toContain('increasecap')
  })
})

describe('LocalHermesEngine', () => {
  it('maps a verified Python bridge response into connected status', async () => {
    const originalBridge = window.sigilDesktop

    window.sigilDesktop = {
      productName: 'Sigil',
      persistenceNamespace: 'com.firecattechnology.sigil',
      brokerSubmissionAvailable: false,
      explainProposal: async () => ({
        ok: false,
        error: 'not_used',
        message: 'Proposal explanation is not used by this status test.'
      }),
      getBackendStatus: async () => ({
        ok: true,
        result: {
          bridge_version: '1',
          status: 'ok',
          mode: 'local-read-only',
          environment: 'paper',
          simulation: true,
          execution_authorized: false,
          broker_submission_available: false,
          supported_commands: ['health']
        }
      })
    }

    try {
      const engine = new LocalHermesEngine()
      const status = await engine.getSystemStatus()

      expect(status.status).toBe('connected')
      expect(status.source).toBe('local')
      expect(status.modelRoute).toBe('python-bridge-v1')
      expect(status.executionAuthorized).toBe(false)
      expect(status.brokerSubmissionAvailable).toBe(false)
    } finally {
      window.sigilDesktop = originalBridge
    }
  })

  it('fails safely when the Python bridge reports an error', async () => {
    const originalBridge = window.sigilDesktop

    window.sigilDesktop = {
      productName: 'Sigil',
      persistenceNamespace: 'com.firecattechnology.sigil',
      brokerSubmissionAvailable: false,
      explainProposal: async () => ({
        ok: false,
        error: 'not_used',
        message: 'Proposal explanation is not used by this status test.'
      }),
      getBackendStatus: async () => ({
        ok: false,
        error: 'backend_unavailable',
        message: 'Backend unavailable.'
      })
    }

    try {
      const engine = new LocalHermesEngine()
      const status = await engine.getSystemStatus()

      expect(status.status).toBe('degraded')
      expect(status.modelRoute).toBe('local-backend-unavailable')
      expect(status.executionAuthorized).toBe(false)
      expect(status.brokerSubmissionAvailable).toBe(false)
    } finally {
      window.sigilDesktop = originalBridge
    }
  })
})
