/**
 * resume-protocol.ts — Magnum #94724 §16 §17
 *
 * Batch resume (O(1) RPC) vs per-session polling (O(N) RPCs).
 * Truncated / epoch-changed → snapshot reconciliation.
 */

export type ResumeRequest = Readonly<{
  epoch: string
  watermarks: Record<string, number>
}>

export type ResumeResult =
  | Readonly<{ kind: 'replay'; events: unknown[]; latest: Record<string, number> }>
  | Readonly<{ kind: 'snapshot-required'; reason: 'truncated' | 'epoch-changed'; sessions: string[] }>

export function isSnapshotRequired(r: ResumeResult): r is Extract<ResumeResult, { kind: 'snapshot-required' }> {
  return r.kind === 'snapshot-required'
}

// Transport vs state reconciliation distinction (§17)
export type ReconnectReadiness =
  | { transportLive: true; stateReconciled: false }
  | { transportLive: true; stateReconciled: true }
  | { transportLive: false; stateReconciled: false }
