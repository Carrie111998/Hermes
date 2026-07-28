import type { SessionInfo } from '@/types/hermes'

import { sessionIdentityKey } from './session-identity'

export interface SidebarSessionEntry {
  branchStem?: string
  session: SessionInfo
}

export interface FlattenSessionsOptions {
  /**
   * Keep the input root order instead of re-sorting by group recency.
   * Use for hand-ordered surfaces (pinned ids, manual recents drag) so a
   * turn completing can't float a row. Branch children still nest under
   * their parent; sibling branches stay ordered by their own recency.
   */
  preserveOrder?: boolean
}

const recency = (session: SessionInfo): number => session.last_active || session.started_at || 0

/** Flat list with branch/fork sessions nested visually under their parent. */
export function flattenSessionsWithBranches(
  sessions: readonly SessionInfo[],
  options: FlattenSessionsOptions = {}
): SidebarSessionEntry[] {
  if (sessions.length < 2) {
    return sessions.map(session => ({ session }))
  }

  const byVisibleId = new Map<string, SessionInfo>()

  for (const session of sessions) {
    byVisibleId.set(sessionIdentityKey(session.id, session.profile), session)
    const rootId = session._lineage_root_id

    if (rootId) {
      byVisibleId.set(sessionIdentityKey(rootId, session.profile), session)
    }
  }

  const childrenByParent = new Map<string, SessionInfo[]>()
  const nestedIds = new Set<string>()

  for (const session of sessions) {
    const parentId = session.parent_session_id

    if (!parentId) {
      continue
    }

    const parent = byVisibleId.get(sessionIdentityKey(parentId, session.profile))

    if (!parent || sessionIdentityKey(parent.id, parent.profile) === sessionIdentityKey(session.id, session.profile)) {
      continue
    }

    const sessionKey = sessionIdentityKey(session.id, session.profile)
    const parentKey = sessionIdentityKey(parent.id, parent.profile)
    nestedIds.add(sessionKey)
    const siblings = childrenByParent.get(parentKey) ?? []
    siblings.push(session)
    childrenByParent.set(parentKey, siblings)
  }

  for (const siblings of childrenByParent.values()) {
    siblings.sort((left, right) => recency(right) - recency(left))
  }

  // A group sorts by its freshest member, so activity on any branch lifts the
  // whole parent→branches cluster together instead of stranding the parent at
  // its own stale timestamp. Memoized — each subtree is folded at most once.
  // Skipped when preserveOrder is set: the caller already chose positions.
  const groupRecencyMemo = new Map<string, number>()

  const groupRecency = (session: SessionInfo): number => {
    const identityKey = sessionIdentityKey(session.id, session.profile)
    const cached = groupRecencyMemo.get(identityKey)

    if (cached !== undefined) {
      return cached
    }

    groupRecencyMemo.set(identityKey, recency(session)) // cycle guard

    const max = (childrenByParent.get(identityKey) ?? []).reduce(
      (acc, child) => Math.max(acc, groupRecency(child)),
      recency(session)
    )

    groupRecencyMemo.set(identityKey, max)

    return max
  }

  // Depth-first so a branch-of-a-branch still renders under its own parent. The
  // `seen` set guards against pathological parent cycles, and the trailing sweep
  // emits anything the walk somehow missed — nothing in the input is ever dropped.
  const out: SidebarSessionEntry[] = []
  const seen = new Set<string>()

  const emit = (session: SessionInfo, branchStem?: string) => {
    const identityKey = sessionIdentityKey(session.id, session.profile)

    if (seen.has(identityKey)) {
      return
    }

    seen.add(identityKey)
    out.push(branchStem ? { branchStem, session } : { session })

    const children = childrenByParent.get(identityKey)
    children?.forEach((child, index) => emit(child, index === children.length - 1 ? '└─ ' : '├─ '))
  }

  const roots = sessions
    .filter(session => !nestedIds.has(sessionIdentityKey(session.id, session.profile)))
    .map((session, index) => ({ index, session }))

  if (!options.preserveOrder) {
    roots.sort((a, b) => groupRecency(b.session) - groupRecency(a.session) || a.index - b.index)
  }

  roots.forEach(({ session }) => emit(session))

  for (const session of sessions) {
    if (!seen.has(sessionIdentityKey(session.id, session.profile))) {
      out.push({ session })
    }
  }

  return out
}
