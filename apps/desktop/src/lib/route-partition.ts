/**
 * route-partition.ts — Magnum #94724 §12 §13
 *
 * Route-partitioned stores: foreground switching is `activePartition =
 * stores.forRoute(newRoute)` instead of `clear sessions; clear cron; ...`.
 *
 * Bounded memory: partitions are LRU/TTL evicted. Hot = foreground, warm =
 * background working, cached = recent inactive, evicted = old inactive.
 */

import type { RouteKey } from '../../electron/connection-route-identity'
import { routeKeyPartitionKey } from '../../electron/connection-route-identity'

export interface Partition<T> {
  route: RouteKey
  scopeKey: string
  data: T
  lastTouchedMs: number
}

export interface PartitionOpts {
  maxPartitions?: number
  ttlMs?: number
}

export class RoutePartitions<T> {
  readonly #map = new Map<string, Partition<T>>()
  readonly #opts: Required<PartitionOpts>

  constructor(
    private readonly createDefault: (route: RouteKey) => T,
    opts: PartitionOpts = {}
  ) {
    this.#opts = {
      maxPartitions: opts.maxPartitions ?? 16,
      ttlMs: opts.ttlMs ?? 30 * 60 * 1000, // 30 min
    }
  }

  forRoute(route: RouteKey): Partition<T> {
    const key = routeKeyPartitionKey(route)
    let p = this.#map.get(key)

    if (p) {
      p.lastTouchedMs = Date.now()
      // LRU: move to end
      this.#map.delete(key)
      this.#map.set(key, p)

      return p
    }

    p = { route, scopeKey: key, data: this.createDefault(route), lastTouchedMs: Date.now() }
    this.#map.set(key, p)
    this.evictIfNeeded()

    return p
  }

  evictIfNeeded(nowMs = Date.now()): string[] {
    const evicted: string[] = []

    // TTL eviction
    for (const [k, p] of [...this.#map.entries()]) {
      if (nowMs - p.lastTouchedMs > this.#opts.ttlMs) {
        this.#map.delete(k)
        evicted.push(k)
      }
    }

    // LRU eviction
    while (this.#map.size > this.#opts.maxPartitions) {
      const oldest = this.#map.keys().next().value as string | undefined

      if (!oldest) {
        break
      }
      this.#map.delete(oldest)
      evicted.push(oldest)
    }

    return evicted
  }

  keys(): string[] { return [...this.#map.keys()] }
  size(): number { return this.#map.size }
  clear(): void { this.#map.clear() }
}
