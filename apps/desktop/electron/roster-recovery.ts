export interface RosterRecoverySignal {
  connectionId: string
}

/**
 * Publish one recovery signal when an enumeration dial that outlived its UI
 * deadline eventually succeeds. Repeated roster consumers can observe the
 * same pending dial, so connection id is the single-flight key.
 */
export function createRosterRecoverySignals(notify: (signal: RosterRecoverySignal) => void) {
  const pending = new Map<string, Promise<void>>()

  return {
    afterPendingDial(connectionId: string, dial: Promise<unknown>): void {
      if (pending.has(connectionId)) {
        return
      }

      const tracked = dial
        .then(() => notify({ connectionId }))
        .catch(() => undefined)
        .finally(() => {
          if (pending.get(connectionId) === tracked) {
            pending.delete(connectionId)
          }
        })

      pending.set(connectionId, tracked)
    }
  }
}
