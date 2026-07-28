import { parseSessionIdentityKey } from '@/lib/session-identity'

/** New ids first, then ids still present in the persisted order. */
export function reconcileFreshFirst(currentIds: string[], orderIds: string[]): string[] {
  const current = new Set(currentIds)
  const retained = orderIds.filter(id => current.has(id))
  const retainedSet = new Set(retained)

  return [...currentIds.filter(id => !retainedSet.has(id)), ...retained]
}

export function resolveManualSessionOrderIds(
  currentIds: string[],
  orderIds: string[],
  manual: boolean,
  hydrating = false,
  scopedProfiles?: readonly string[]
): string[] {
  if (!manual || !orderIds.length) {
    return []
  }

  if (hydrating) {
    return orderIds
  }

  const profilesInScope = new Set(
    scopedProfiles ?? currentIds.map(id => parseSessionIdentityKey(id).profile)
  )

  const isInScope = (id: string) => profilesInScope.has(parseSessionIdentityKey(id).profile)
  const scopedOrderIds = orderIds.filter(isInScope)
  const hiddenOrderIds = orderIds.filter(id => !isInScope(id))

  if (!currentIds.length) {
    return scopedProfiles ? hiddenOrderIds : []
  }

  const current = new Set(currentIds)
  const retained = scopedOrderIds.filter(id => current.has(id))

  if (!retained.length) {
    if (!scopedOrderIds.length) {
      return orderIds
    }

    return hiddenOrderIds
  }

  return mergeScopedSessionOrderIds(orderIds, reconcileFreshFirst(currentIds, scopedOrderIds), [...profilesInScope])
}

/** Merge a drag order for the profiles represented by `scopedIds` without
 * erasing persisted order for profiles whose rows are not currently visible. */
export function mergeScopedSessionOrderIds(
  orderIds: string[],
  scopedIds: string[],
  scopedProfiles?: readonly string[]
): string[] {
  if (!scopedIds.length && !scopedProfiles?.length) {
    return orderIds
  }

  const nextScopedIds = [...new Set(scopedIds)]

  const profilesInScope = new Set([
    ...(scopedProfiles ?? []),
    ...nextScopedIds.map(id => parseSessionIdentityKey(id).profile)
  ])

  const merged: string[] = []
  let inserted = false

  for (const id of orderIds) {
    if (profilesInScope.has(parseSessionIdentityKey(id).profile)) {
      if (!inserted) {
        merged.push(...nextScopedIds)
        inserted = true
      }

      continue
    }

    merged.push(id)
  }

  if (!inserted) {
    merged.push(...nextScopedIds)
  }

  return merged
}

/** Reorder `items` by `orderIds`; items missing from the order surface first. */
export function orderByIds<T>(items: T[], getId: (item: T) => string, orderIds: string[]): T[] {
  if (!orderIds.length) {
    return items
  }

  const byId = new Map(items.map(item => [getId(item), item]))
  const seen = new Set<string>()
  const ordered: T[] = []

  for (const id of orderIds) {
    const item = byId.get(id)

    if (item) {
      ordered.push(item)
      seen.add(id)
    }
  }

  // Items missing from the persisted order are new since it was last
  // reconciled. Callers pass recency-sorted lists (newest first), so surface
  // these at the TOP instead of burying them beneath the saved order —
  // otherwise a brand-new session sinks to the bottom of the sidebar and reads
  // as "my latest session never showed up".
  const fresh = items.filter(item => !seen.has(getId(item)))

  return fresh.length ? [...fresh, ...ordered] : ordered
}

/** Reconcile a persisted order against the live id set (fresh-first). */
export function reconcileOrderIds(currentIds: string[], orderIds: string[]): string[] {
  if (!currentIds.length) {
    return []
  }

  if (!orderIds.length) {
    return currentIds
  }

  return reconcileFreshFirst(currentIds, orderIds)
}

/** True when two id lists are element-for-element identical. */
export function sameIds(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index])
}
