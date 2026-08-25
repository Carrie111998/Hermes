import type { SessionInfo } from '@/hermes'

import type { SidebarSessionGroup } from '@/app/chat/sidebar/projects/workspace-groups'

/**
 * Session taxonomy grouping (pure — no store access, unit-testable).
 *
 * The sidebar organizes sessions by their approved taxonomy:
 *   disposition   -> bucket (project / archive; transient + junk are hidden)
 *   project_group -> category (e.g. "Hermes Community Extensions")
 *   project       -> named project (e.g. "Fusion Router")
 *
 * The Projects area renders one collapsible section per project_group; inside
 * it, named projects become sub-groups (Hermes Community Extensions expands to
 * Fusion Router / Thin Remote / New Tab / Agora). Categories with no named
 * projects render their sessions flat. Archives renders the same shape with
 * disposition=archive rows.
 */

export interface TaxonomyGroup {
  /** project_group value (category bucket). */
  id: string
  label: string
  /** Sessions in this category that have no named project (flat rows). */
  flat: SessionInfo[]
  /** Named projects within the category, ordered by recency of newest row. */
  projects: SidebarSessionGroup[]
  /** All sessions in this category (flat + inside projects). */
  sessions: SessionInfo[]
}

export interface TaxonomyView {
  groups: TaxonomyGroup[]
  /** Sessions with no disposition at all (unclassified) — the Recent fallback. */
  unclassified: SessionInfo[]
  /** Total project-group categories rendered. */
  categoryCount: number
}

const categoryLabel = (group: null | string | undefined): string =>
  group && group.trim().length > 0 ? group.trim() : UNFILED_GROUP

/** Sentinel id for sessions with no project_group (localized at render). */
export const UNFILED_GROUP = '__unfiled__'

const sessionTime = (session: SessionInfo): number =>
  session.last_active || session.started_at || 0

/** Group sessions by project_group (category) then project (named), newest first. */
export function groupByTaxonomy(sessions: SessionInfo[]): TaxonomyView {
  const byCategory = new Map<string, SessionInfo[]>()

  for (const session of sessions) {
    // Only classified rows belong to the taxonomy buckets; anything without a
    // disposition is the unclassified fallback (Recent).
    const disp = session.disposition
    if (!disp) {
      continue
    }
    const key = categoryLabel(session.project_group)
    const list = byCategory.get(key) ?? []
    list.push(session)
    byCategory.set(key, list)
  }

  const groups: TaxonomyGroup[] = [...byCategory.entries()]
    .map(([label, categorySessions]) => {
      const byProject = new Map<string, SessionInfo[]>()
      for (const session of categorySessions) {
        const project = session.project?.trim()
        if (!project) {
          continue
        }
        const list = byProject.get(project) ?? []
        list.push(session)
        byProject.set(project, list)
      }

      const projects: SidebarSessionGroup[] = [...byProject.entries()]
        .map(([projectLabel, projectSessions]) => ({
          id: `${label}::${projectLabel}`,
          label: projectLabel,
          path: null,
          sessions: [...projectSessions].sort((a, b) => sessionTime(b) - sessionTime(a))
        }))
        .sort((a, b) => sessionTime(b.sessions[0]) - sessionTime(a.sessions[0]))

      const flat = categorySessions.filter(s => !s.project?.trim())

      return {
        id: label,
        label,
        flat,
        projects,
        sessions: categorySessions
      }
    })
    .sort((a, b) => sessionTime(b.sessions[0]) - sessionTime(a.sessions[0]))

  const unclassified = sessions.filter(s => !s.disposition)

  return {
    groups,
    unclassified,
    categoryCount: groups.length
  }
}

/** Sessions that belong to a taxonomy bucket (disposition set). */
export function hasDisposition(session: SessionInfo): boolean {
  return Boolean(session.disposition)
}

/** Sessions with no disposition — the unclassified fallback (Recent). */
export function unclassifiedSessions(sessions: SessionInfo[]): SessionInfo[] {
  return sessions.filter(s => !s.disposition)
}

/** Sessions that should never surface in the sidebar (transient/junk). */
export function isHiddenDisposition(session: SessionInfo): boolean {
  const disp = session.disposition
  return disp === 'transient' || disp === 'junk'
}
