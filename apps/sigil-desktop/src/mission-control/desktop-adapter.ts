import type { SigilOperatorAdapter, SigilSnapshot, SimulatedOperatorAction } from './types'

function currency(value: string): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(value))
}

function mapRuntime(runtime: SigilRuntimeSnapshot): SigilSnapshot {
  return {
    revision: runtime.revision,
    dataState: runtime.connection.status === 'connected' ? 'ready' : 'stale',
    lastUpdated: runtime.connection.last_refresh_at,
    degradedServices: runtime.connection.degraded_services,
    environment: 'paper',
    simulation: true,
    brokerConnection: runtime.connection.status === 'connected' ? 'connected' : 'disconnected',
    maskedAccountId: 'PAPER LOCAL',
    systemHealth: runtime.connection.status === 'connected' ? 'Runtime connected' : 'Runtime degraded',
    cash: currency(runtime.balances.cash),
    portfolioValue: currency(runtime.balances.portfolio_value),
    positions: runtime.positions.map(position => ({
      symbol: position.symbol,
      quantity: position.quantity,
      marketValue: currency(position.market_value)
    })),
    automation: {
      state: runtime.automation.state,
      cycleCount: runtime.automation.cycle_count,
      lastCycleAt: runtime.automation.last_cycle_at,
      nextCycleAt: runtime.automation.next_cycle_at
    },
    activeStrategies: runtime.automation.state === 'running' ? 1 : 0,
    pendingApprovals: runtime.proposals.filter(proposal => proposal.status === 'pending').length,
    killSwitch: 'armed',
    certificationStatus: 'Paper runtime',
    maximumLaunchNotional: '$25.00',
    firstLaunchLimit: '$25.00',
    launchState: 'suspended',
    stages: [
      { id: 'backend', label: 'Backend bridge', detail: runtime.connection.status, state: 'complete' },
      { id: 'state', label: 'Runtime state', detail: `Revision ${runtime.revision}`, state: 'complete' },
      { id: 'analysis', label: 'Hermes analysis', detail: 'Proposal only', state: 'ready' },
      { id: 'proposal', label: 'Proposal generation', detail: runtime.automation.state, state: 'pending' },
      { id: 'approval', label: 'Approval', detail: 'Operator unavailable', state: 'blocked' },
      { id: 'execution', label: 'Paper execution', detail: 'Disabled', state: 'blocked' },
      { id: 'broker', label: 'Broker transport', detail: 'Unavailable', state: 'blocked' }
    ],
    proposals: runtime.proposals.map(proposal => ({
      id: proposal.id,
      symbol: proposal.symbol,
      side: proposal.side,
      quantity: proposal.quantity,
      estimatedNotional: currency(proposal.estimated_notional),
      strategy: proposal.strategy,
      status: proposal.status,
      evidenceReferences: proposal.evidence_references,
      riskResults: proposal.risk_results
    })),
    receipts: runtime.executions.map(execution => {
      const reconciliation = runtime.reconciliation.find(item => item.order_id === execution.order_id)

      return {
        id: execution.id,
        orderId: execution.order_id,
        symbol: execution.symbol,
        brokerStatus: execution.status,
        state: 'simulated',
        duplicatePrevention: reconciliation?.status ?? 'local paper identity retained',
        reconciliationRequired: reconciliation?.required ?? false,
        timestamp: execution.timestamp
      }
    }),
    auditEvents: runtime.audit.map(event => ({
      id: event.id,
      timestamp: event.timestamp,
      orderId: event.order_id,
      proposalId: event.proposal_id,
      status: event.status,
      evidenceReference: event.evidence_reference,
      summary: event.summary,
      details: event.details
    }))
  }
}

export class DesktopSigilOperatorAdapter implements SigilOperatorAdapter {
  async readSnapshot(): Promise<SigilSnapshot> {
    const response = await window.sigilDesktop?.getRuntimeSnapshot?.()

    if (!response?.ok) {
      throw new Error(response?.message ?? 'The governed local backend bridge is unavailable.')
    }

    return mapRuntime(response.result)
  }

  async controlPaperCycle(action: 'start' | 'pause' | 'stop'): Promise<SigilSnapshot> {
    const response = await window.sigilDesktop?.controlPaperCycle?.(action)

    if (!response?.ok) {
      throw new Error(response?.message ?? 'Paper automation control failed safely.')
    }

    return mapRuntime(response.result)
  }

  async applySimulatedAction(_action: SimulatedOperatorAction): Promise<SigilSnapshot> {
    throw new Error('Approvals and execution actions are disabled in the live paper runtime.')
  }
}

export const desktopSigilOperatorAdapter = new DesktopSigilOperatorAdapter()
