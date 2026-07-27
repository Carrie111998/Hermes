import fs from 'node:fs'
import path from 'node:path'

export interface ActivationEvidence {
  httpReady: true
  websocketReady: true
  profile: string | null
}

export interface ActivationReceipt extends ActivationEvidence {
  stage: 'activation'
  state: 'completed'
  verifiedAt: string
}

export interface ActivationReceiptDeps {
  now?: () => Date
}

export type ActivationRollbackState = 'not-attempted' | 'completed' | 'failed'

export interface ActivationFailureEvidence {
  error: string
  httpReady: boolean
  websocketReady: boolean
  rollbackState: ActivationRollbackState
}

export interface ActivationFailureReceipt extends ActivationFailureEvidence {
  stage: 'activation'
  state: 'failed'
  verifiedAt: string
}

function writeReceiptAtomically<T>(receiptPath: string, receipt: T): T {
  const tempPath = `${receiptPath}.tmp`

  fs.mkdirSync(path.dirname(receiptPath), { recursive: true })
  fs.writeFileSync(tempPath, `${JSON.stringify(receipt, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 })
  fs.renameSync(tempPath, receiptPath)

  return receipt
}

/** Atomically persist the terminal evidence required before update activation succeeds. */
export function writeActivationReceipt(
  receiptPath: string,
  evidence: ActivationEvidence,
  deps: ActivationReceiptDeps = {}
): ActivationReceipt {
  const receipt: ActivationReceipt = {
    stage: 'activation',
    state: 'completed',
    verifiedAt: (deps.now ?? (() => new Date()))().toISOString(),
    ...evidence
  }

  return writeReceiptAtomically(receiptPath, receipt)
}

/** Persist an honest terminal state when first boot fails before activation. */
export function writeActivationFailureReceipt(
  receiptPath: string,
  evidence: ActivationFailureEvidence,
  deps: ActivationReceiptDeps = {}
): ActivationFailureReceipt {
  const receipt: ActivationFailureReceipt = {
    stage: 'activation',
    state: 'failed',
    verifiedAt: (deps.now ?? (() => new Date()))().toISOString(),
    ...evidence
  }

  return writeReceiptAtomically(receiptPath, receipt)
}
