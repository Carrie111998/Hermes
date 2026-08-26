/**
 * retention-lease.ts — Magnum #94724 §8
 *
 * Explicit retention ownership: every reason a gateway stays alive is a
 * counted lease with a release handle. The pruner may evict a route only
 * when no legitimate owner still holds it.
 *
 * Intended shape in main/gateway.ts:
 *   const lease = retention.acquire(route, { ownerId, kind: 'bot-tile' })
 *   // ... later:
 *   lease.release()
 *
 * Query:
 *   retention.countFor(route)                  → number
 *   retention.ownersFor(route)                 → {ownerId, kind}[] (observable)
 *   retention.mayPrune(route, {activeRequests, activeTurns, isForeground}) → boolean
 */

import type { RouteKey } from './connection-route-identity'
import { supervisorKey } from './gateway-supervisor'

export type RetentionKind = 'bot-tile' | 'terminal-pane' | 'active-turn' | 'background-job' | 'activation-lease' | 'relay' | 'foreground' | string

export interface RetentionOwner {
  kind: RetentionKind
  ownerId: string
}

export interface RetentionLease {
  readonly key: string
  readonly owner: RetentionOwner
  release(): boolean
}

export interface MayPruneSignals {
  activeRequests?: number
  activeTurns?: number
  isForeground?: boolean
}

export class RetentionRegistry {
  readonly #leases = new Map<string, Map<string, RetentionOwner>>()

  acquire(route: RouteKey, owner: RetentionOwner): RetentionLease {
    const sk = supervisorKey(route)
    let bucket = this.#leases.get(sk)

    if (!bucket) {
      bucket = new Map()
      this.#leases.set(sk, bucket)
    }

    // ownerId is globally unique per owner instance; re-acquire with same id is idempotent
    bucket.set(owner.ownerId, owner)
    const key = sk
    let released = false

    return {
      key,
      owner,
      release: (): boolean => {
        if (released) {
          return false
        }
        released = true
        const b = this.#leases.get(key)

        if (!b) {
          return false
        }
        const deleted = b.delete(owner.ownerId)

        if (b.size === 0) {
          this.#leases.delete(key)
        }

        return deleted
      },
    }
  }

  countFor(route: RouteKey): number {
    return this.#leases.get(supervisorKey(route))?.size ?? 0
  }

  ownersFor(route: RouteKey): RetentionOwner[] {
    const bucket = this.#leases.get(supervisorKey(route))

    return bucket ? [...bucket.values()] : []
  }

  /** Whether the pruner is allowed to evict this route. */
  mayPrune(route: RouteKey, signals: MayPruneSignals = {}): boolean {
    if ((this.#leases.get(supervisorKey(route))?.size ?? 0) > 0) {
      return false
    }

    if ((signals.activeRequests ?? 0) > 0) {
      return false
    }

    if ((signals.activeTurns ?? 0) > 0) {
      return false
    }

    if (signals.isForeground) {
      return false
    }

    return true
  }

  // Diagnostic: all supervisor keys currently retained
  retainedKeys(): string[] {
    return [...this.#leases.keys()]
  }
}
