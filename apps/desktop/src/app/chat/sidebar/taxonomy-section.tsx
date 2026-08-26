import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useMemo, useState } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { SidebarGroup, SidebarGroupContent } from '@/components/ui/sidebar'
import type { SessionInfo } from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { groupByTaxonomy, UNFILED_GROUP, type TaxonomyView } from '@/lib/session-taxonomy'
import { sessionPinId, $projectSessions, $archiveSessions } from '@/store/session'

import { SidebarGroupRow, SidebarRowLeadGlyph, SidebarRowStack } from './chrome'
import { SidebarSectionHeader } from './sessions-section'
import { SidebarSessionRow } from './session-row'

/**
 * Taxonomy sidebar sections — the primary organization of the session rail.
 *
 * Projects (disposition=project, active work) and Archives (disposition=archive,
 * finished work) each render as a collapsible section. Inside, sessions nest
 * two levels: project_group (category, e.g. "Hermes Community Extensions") ->
 * named project (e.g. "Fusion Router") -> sessions. Categories without a named
 * project render their sessions flat under the category header.
 *
 * Transient and junk sessions never reach these stores (the backend slices
 * exclude them), so the sidebar simply has no section for them.
 */

interface TaxonomySectionProps {
  activeSessionId: null | string
  onArchiveSession: (sessionId: string) => void
  onDeleteSession: (sessionId: string) => void
  onResumeSession: (sessionId: string) => void
  onTogglePin: (sessionId: string) => void
  onToggleUnread: (sessionId: string) => void
  /** Sessions pinned by the user live ONLY in the Pinned section — never also
   *  under their taxonomy group. The sidebar's other lists apply the same
   *  rule, so the taxonomy sections must too. */
  isPinned?: (session: SessionInfo) => boolean
  rootClassName?: string
}

interface TaxonomyRowHandlers {
  activeSessionId: null | string
  onArchiveSession: (sessionId: string) => void
  onDeleteSession: (sessionId: string) => void
  onResumeSession: (sessionId: string) => void
  onTogglePin: (sessionId: string) => void
  onToggleUnread: (sessionId: string) => void
}

function SessionRows({ sessions, handlers }: { sessions: SessionInfo[]; handlers: TaxonomyRowHandlers }) {
  return (
    <SidebarRowStack>
      {sessions.map(session => (
        <SidebarSessionRow
          isPinned={false}
          isSelected={session.id === handlers.activeSessionId}
          key={session.id}
          onArchive={() => handlers.onArchiveSession(session.id)}
          onDelete={() => handlers.onDeleteSession(session.id)}
          onPin={() => handlers.onTogglePin(sessionPinId(session))}
          onResume={() => handlers.onResumeSession(session.id)}
          onToggleUnread={() => handlers.onToggleUnread(session.id)}
          session={session}
          unread={session.unread === true}
        />
      ))}
    </SidebarRowStack>
  )
}

/** One collapsible category: "Hermes Community Extensions" -> projects/sessions. */
function TaxonomyCategory({
  category,
  handlers,
  openByDefault
}: {
  category: TaxonomyView['groups'][number]
  handlers: TaxonomyRowHandlers
  openByDefault: boolean
}) {
  const [open, setOpen] = useState(openByDefault)
  const { t } = useI18n()

  // A category with named projects nests them as sub-rows; a category with no
  // named project renders its sessions directly under the category header.
  const hasNamedProjects = category.projects.length > 0
  const label =
    category.id === UNFILED_GROUP
      ? (t.sidebar.taxonomy?.unfiled ?? 'Unfiled')
      : category.label

  return (
    <div className="flex flex-col gap-px">
      <SidebarGroupRow
        label={<span className="truncate text-[0.8125rem]">{label}</span>}
        lead={
          <SidebarRowLeadGlyph>
            <Codicon
              className="text-(--ui-text-tertiary)"
              name={hasNamedProjects ? 'project' : 'files'}
              size="0.75rem"
            />
          </SidebarRowLeadGlyph>
        }
        toggle={{
          ariaLabel: category.label,
          onToggle: () => setOpen(prev => !prev),
          open
        }}
      />
      {open &&
        (hasNamedProjects ? (
          <div className="flex flex-col gap-px pl-3">
            {category.projects.map(project => (
              <TaxonomyProject key={project.id} project={project} handlers={handlers} />
            ))}
            {category.flat.map(session => (
              <SidebarSessionRow
                isPinned={false}
                isSelected={session.id === handlers.activeSessionId}
                key={session.id}
                onArchive={() => handlers.onArchiveSession(session.id)}
                onDelete={() => handlers.onDeleteSession(session.id)}
                onPin={() => handlers.onTogglePin(sessionPinId(session))}
                onResume={() => handlers.onResumeSession(session.id)}
                onToggleUnread={() => handlers.onToggleUnread(session.id)}
                session={session}
                unread={session.unread === true}
              />
            ))}
          </div>
        ) : (
          <div className="pl-3">
            <SessionRows sessions={category.sessions} handlers={handlers} />
          </div>
        ))}
    </div>
  )
}

/** One collapsible named project inside a category: "Fusion Router" -> sessions. */
function TaxonomyProject({
  project,
  handlers
}: {
  project: { id: string; label: string; sessions: SessionInfo[] }
  handlers: TaxonomyRowHandlers
}) {
  const [open, setOpen] = useState(true)

  return (
    <div className="flex flex-col gap-px">
      <SidebarGroupRow
        label={<span className="truncate text-[0.8125rem]">{project.label}</span>}
        lead={
          <SidebarRowLeadGlyph>
            <Codicon className="text-(--ui-text-tertiary)" name="rocket" size="0.75rem" />
          </SidebarRowLeadGlyph>
        }
        toggle={{
          ariaLabel: project.label,
          onToggle: () => setOpen(prev => !prev),
          open
        }}
      />
      {open && (
        <div className="pl-3">
          <SessionRows sessions={project.sessions} handlers={handlers} />
        </div>
      )}
    </div>
  )
}

export function TaxonomySidebarSections({
  activeSessionId,
  onArchiveSession,
  onDeleteSession,
  onResumeSession,
  onTogglePin,
  onToggleUnread,
  isPinned,
  rootClassName
}: TaxonomySectionProps) {
  const { t } = useI18n()
  const s = t.sidebar
  const projectSessions = useStore($projectSessions)
  const archiveSessions = useStore($archiveSessions)

  // Pins live only in the Pinned section. Filter them out of the taxonomy
  // slices BEFORE grouping so a pinned project session doesn't render twice
  // (once in Pinned, once under its project).
  const unpinnedProjectSessions = useMemo(
    () => (isPinned ? projectSessions.filter(s => !isPinned(s)) : projectSessions),
    [projectSessions, isPinned]
  )
  const unpinnedArchiveSessions = useMemo(
    () => (isPinned ? archiveSessions.filter(s => !isPinned(s)) : archiveSessions),
    [archiveSessions, isPinned]
  )

  const projectsView = useMemo(() => groupByTaxonomy(unpinnedProjectSessions), [unpinnedProjectSessions])
  const archivesView = useMemo(() => groupByTaxonomy(unpinnedArchiveSessions), [unpinnedArchiveSessions])

  const [projectsOpen, setProjectsOpen] = useState(true)
  const [archivesOpen, setArchivesOpen] = useState(true)

  const handlers: TaxonomyRowHandlers = {
    activeSessionId,
    onArchiveSession,
    onDeleteSession,
    onResumeSession,
    onTogglePin,
    onToggleUnread
  }

  if (projectsView.categoryCount === 0 && archivesView.categoryCount === 0) {
    return null
  }

  return (
    <div className={cn('flex flex-col gap-px', rootClassName)}>
      {projectsView.categoryCount > 0 && (
        <SidebarGroup className="p-0">
          <SidebarSectionHeader
            label={s.taxonomy?.projects ?? 'Projects'}
            onToggle={() => setProjectsOpen(prev => !prev)}
            open={projectsOpen}
          />
          <SidebarGroupContent>
            {projectsOpen && (
              <div className="flex flex-col gap-px">
                {projectsView.groups.map(category => (
                  <TaxonomyCategory
                    category={category}
                    handlers={handlers}
                    key={category.id}
                    openByDefault
                  />
                ))}
              </div>
            )}
          </SidebarGroupContent>
        </SidebarGroup>
      )}

      {archivesView.categoryCount > 0 && (
        <SidebarGroup className="p-0">
          <SidebarSectionHeader
            label={s.taxonomy?.archives ?? 'Archives'}
            onToggle={() => setArchivesOpen(prev => !prev)}
            open={archivesOpen}
          />
          <SidebarGroupContent>
            {archivesOpen && (
              <div className="flex flex-col gap-px">
                {archivesView.groups.map(category => (
                  <TaxonomyCategory
                    category={category}
                    handlers={handlers}
                    key={category.id}
                    openByDefault={false}
                  />
                ))}
              </div>
            )}
          </SidebarGroupContent>
        </SidebarGroup>
      )}
    </div>
  )
}
