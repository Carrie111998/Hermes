/**
 * Intentional backend-exit notice suppression for desktop.
 *
 * Soft re-homes (gateway-mode apply) already use `softRehomeInProgress`.
 * Retry, profile-switch, and soft-apply tear-downs invalidate
 * `backendConnectionState` first, so their child exits are rejected as stale
 * before `sendBackendExit` — that stale-owner protection stays the primary
 * guard for those flows.
 *
 * The Windows update/uninstall lock-release path kills the live primary child
 * after capturing PIDs, then invalidates connection state so a late exit is
 * stale-owned. An intentional teardown depth additionally covers straggler
 * kills of a briefly re-attached current process during the unlock wait.
 */

export type BackendExitSuppressionContext = {
  softRehomeInProgress: boolean
  intentionalTeardownDepth: number
}

/**
 * Whether `sendBackendExit` should stay silent for this exit.
 * Soft re-home and an in-flight intentional lock-release teardown both suppress;
 * an unexpected current-backend exit (depth 0, soft false) does not.
 */
export function shouldSuppressBackendExitNotice(context: BackendExitSuppressionContext): boolean {
  return context.softRehomeInProgress || context.intentionalTeardownDepth > 0
}

export type IntentionalBackendTeardownGuard = {
  readonly depth: number
  begin(): void
  end(): void
  run<T>(fn: () => Promise<T>): Promise<T>
}

/** Nestable counter held for the duration of an intentional lock-release kill. */
export function createIntentionalBackendTeardownGuard(): IntentionalBackendTeardownGuard {
  let depth = 0

  return {
    get depth() {
      return depth
    },

    begin() {
      depth += 1
    },

    end() {
      depth = Math.max(0, depth - 1)
    },

    async run<T>(fn: () => Promise<T>): Promise<T> {
      depth += 1

      try {
        return await fn()
      } finally {
        depth = Math.max(0, depth - 1)
      }
    }
  }
}
