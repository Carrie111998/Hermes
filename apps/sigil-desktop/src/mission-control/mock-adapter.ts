import type {
  AuditEvent,
  SigilDataState,
  SigilOperatorAdapter,
  SigilSnapshot,
  SimulatedOperatorAction
} from './types'

const BASE_TIME = '2026-07-25T14:32:18Z'

export const SIGIL_FIRST_LAUNCH_LIMIT = '$25.00'

export const INITIAL_SIGIL_SNAPSHOT: SigilSnapshot = {
  dataState: 'stale',
  lastUpdated: BASE_TIME,
  environment: 'paper',
  simulation: true,
  brokerConnection: 'disconnected',
  maskedAccountId: '•••• 23F4',
  systemHealth: 'Governance healthy',
  cash: '$10,000.00',
  portfolioValue: '$10,842.16',
  activeStrategies: 4,
  pendingApprovals: 2,
  killSwitch: 'armed',
  certificationStatus: 'Paper certified',
  maximumLaunchNotional: SIGIL_FIRST_LAUNCH_LIMIT,
  firstLaunchLimit: SIGIL_FIRST_LAUNCH_LIMIT,
  launchState: 'suspended',
  stages: [
    { id: 'strategy', label: 'Strategy', detail: 'Qualified', state: 'complete' },
    { id: 'risk', label: 'Risk', detail: 'Passed', state: 'complete' },
    { id: 'capital', label: 'Capital controls', detail: '$25 cap', state: 'complete' },
    { id: 'eligibility', label: 'Eligibility', detail: 'Eligible', state: 'complete' },
    { id: 'certification', label: 'Certification', detail: 'Paper only', state: 'complete' },
    { id: 'launch', label: 'Launch control', detail: 'Suspended', state: 'pending' },
    { id: 'admission', label: 'Order admission', detail: 'Awaiting', state: 'pending' },
    { id: 'handoff', label: 'Execution handoff', detail: 'Not started', state: 'blocked' },
    { id: 'submission', label: 'Broker submission', detail: 'Unavailable', state: 'blocked' }
  ],
  proposals: [
    {
      id: 'PRP-20260725-0042',
      symbol: 'MSFT',
      side: 'BUY',
      quantity: 0.05,
      estimatedNotional: '$22.64',
      strategy: 'Quality momentum v2',
      status: 'pending',
      evidenceReferences: ['EVD-9F3A7B1C', 'RISK-42A'],
      riskResults: ['Position limit passed', 'Liquidity check passed', 'Concentration 2.1%']
    },
    {
      id: 'PRP-20260725-0041',
      symbol: 'NVDA',
      side: 'SELL',
      quantity: 0.1,
      estimatedNotional: '$17.30',
      strategy: 'Volatility reduction v1',
      status: 'pending',
      evidenceReferences: ['EVD-7C2D9A6E', 'RISK-419'],
      riskResults: ['Sellable quantity passed', 'Wash-sale review clear']
    },
    {
      id: 'PRP-20260725-0040',
      symbol: 'AAPL',
      side: 'BUY',
      quantity: 0.08,
      estimatedNotional: '$17.12',
      strategy: 'Quality momentum v2',
      status: 'rejected',
      evidenceReferences: ['EVD-31F09CC2'],
      riskResults: ['Rejected: stale market evidence']
    }
  ],
  receipts: [
    {
      id: 'RCT-20260725-018',
      orderId: 'ORD-20260725-018',
      symbol: 'MSFT',
      brokerStatus: 'Simulated acknowledgement',
      state: 'simulated',
      duplicatePrevention: 'Envelope consumed once',
      reconciliationRequired: false,
      timestamp: '2026-07-25T14:18:03Z'
    },
    {
      id: 'RCT-20260725-017',
      orderId: 'ORD-20260725-017',
      symbol: 'AAPL',
      brokerStatus: 'Rejected before transport',
      state: 'rejected',
      duplicatePrevention: 'Client order ID retained',
      reconciliationRequired: false,
      timestamp: '2026-07-25T13:41:22Z'
    },
    {
      id: 'RCT-20260725-016',
      orderId: 'ORD-20260725-016',
      symbol: 'NVDA',
      brokerStatus: 'Outcome uncertain — no retry',
      state: 'outcome-uncertain',
      duplicatePrevention: 'Retry blocked pending reconciliation',
      reconciliationRequired: true,
      timestamp: '2026-07-25T12:56:09Z'
    }
  ],
  auditEvents: [
    {
      id: 'AUD-20260725-108',
      timestamp: '2026-07-25T14:18:03Z',
      orderId: 'ORD-20260725-018',
      proposalId: 'PRP-20260725-0042',
      status: 'simulated',
      evidenceReference: 'EVD-9F3A7B1C',
      summary: 'Immutable simulated receipt recorded',
      details: {
        event_type: 'execution_receipt_recorded',
        paper_only: true,
        receipt_digest: 'sha256:8d92…bf31',
        broker_submission_attempted: false
      }
    },
    {
      id: 'AUD-20260725-107',
      timestamp: '2026-07-25T13:41:22Z',
      orderId: 'ORD-20260725-017',
      proposalId: 'PRP-20260725-0040',
      status: 'rejected',
      evidenceReference: 'EVD-31F09CC2',
      summary: 'Submission rejected before transport',
      details: {
        event_type: 'broker_submission_rejected',
        blocker: 'market_evidence_stale',
        broker_submission_attempted: false
      }
    },
    {
      id: 'AUD-20260725-106',
      timestamp: '2026-07-25T12:56:09Z',
      orderId: 'ORD-20260725-016',
      proposalId: 'PRP-20260725-0039',
      status: 'outcome-uncertain',
      evidenceReference: 'EVD-1158A0D2',
      summary: 'Retry blocked; reconciliation required',
      details: {
        event_type: 'submission_outcome_uncertain',
        duplicate_prevention: 'active',
        automatic_retry_allowed: false
      }
    }
  ]
}

function actionEvent(action: SimulatedOperatorAction): AuditEvent {
  const proposalId = 'proposalId' in action ? action.proposalId : '—'
  const verb = action.type.replaceAll('-', ' ')

  return {
    id: `AUD-SIM-${action.type}`,
    timestamp: BASE_TIME,
    orderId: '—',
    proposalId,
    status: 'simulated',
    evidenceReference: 'SIMULATED-UI',
    summary: `Operator ${verb} recorded in local simulation`,
    details: {
      event_type: action.type,
      simulation_only: true,
      broker_submission_attempted: false
    }
  }
}

function applyAction(snapshot: SigilSnapshot, action: SimulatedOperatorAction): SigilSnapshot {
  const proposals = snapshot.proposals.map(proposal => {
    if (!('proposalId' in action) || proposal.id !== action.proposalId) {
      return proposal
    }

    return {
      ...proposal,
      status: action.type === 'approve-proposal' ? ('approved' as const) : ('rejected' as const)
    }
  })

  return {
    ...snapshot,
    proposals,
    pendingApprovals: proposals.filter(proposal => proposal.status === 'pending').length,
    launchState:
      action.type === 'arm-launch' ? 'armed' : action.type === 'suspend-launch' ? 'suspended' : snapshot.launchState,
    killSwitch: action.type === 'engage-kill-switch' ? 'engaged' : snapshot.killSwitch,
    auditEvents: [actionEvent(action), ...snapshot.auditEvents]
  }
}

export class MockSigilOperatorAdapter implements SigilOperatorAdapter {
  private snapshot: SigilSnapshot

  constructor(dataState: SigilDataState = INITIAL_SIGIL_SNAPSHOT.dataState) {
    this.snapshot = {
      ...INITIAL_SIGIL_SNAPSHOT,
      dataState,
      proposals: dataState === 'empty' ? [] : INITIAL_SIGIL_SNAPSHOT.proposals,
      receipts: dataState === 'empty' ? [] : INITIAL_SIGIL_SNAPSHOT.receipts,
      auditEvents: dataState === 'empty' ? [] : INITIAL_SIGIL_SNAPSHOT.auditEvents
    }
  }

  async readSnapshot(): Promise<SigilSnapshot> {
    if (this.snapshot.dataState === 'error') {
      throw new Error('The local Sigil snapshot could not be verified.')
    }

    return structuredClone(this.snapshot)
  }

  async applySimulatedAction(action: SimulatedOperatorAction): Promise<SigilSnapshot> {
    this.snapshot = applyAction(this.snapshot, action)

    return structuredClone(this.snapshot)
  }
}

export const sigilOperatorAdapter: SigilOperatorAdapter = new MockSigilOperatorAdapter()
