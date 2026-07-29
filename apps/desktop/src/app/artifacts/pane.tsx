import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { getSessionMessages } from '@/hermes'
import { useI18n } from '@/i18n'
import { FileImage, FileText, Link2, Loader2, RefreshCw } from '@/lib/icons'
import { downloadGatewayMediaFile, isRemoteGateway } from '@/lib/media'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import { $activeProjectId, $projectScope, $projectTree } from '@/store/projects'
import { $currentCwd, $sessions, sessionMatchesStoredId } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { ARTIFACTS_ROUTE } from '../routes'

import {
  type ArtifactRecord,
  artifactSessionsForProject,
  collectArtifactsForSession,
  preferredArtifactProjectId
} from './artifact-utils'
import { $artifactsPaneOpen } from './pane-state'

const PANE_PROJECT_SESSIONS = 4
const PANE_SECTION_ITEMS = 6

async function collectSessionArtifacts(sessions: readonly SessionInfo[]): Promise<ArtifactRecord[]> {
  const results = await Promise.allSettled(sessions.map(session => getSessionMessages(session.id, session.profile)))

  return results
    .flatMap((result, index) =>
      result.status === 'fulfilled' ? collectArtifactsForSession(sessions[index], result.value.messages) : []
    )
    .sort((left, right) => right.timestamp - left.timestamp)
}

async function openArtifactHref(href: string): Promise<void> {
  if (isRemoteGateway() && /^file:/i.test(href)) {
    await downloadGatewayMediaFile(href)

    return
  }

  if (window.hermesDesktop?.openExternal) {
    await window.hermesDesktop.openExternal(href)
  } else {
    window.open(href, '_blank', 'noopener,noreferrer')
  }
}

export function ArtifactsPane() {
  const { t } = useI18n()
  const navigate = useNavigate()
  const open = useStore($artifactsPaneOpen)
  const projectTree = useStore($projectTree)
  const projectScope = useStore($projectScope)
  const activeProjectId = useStore($activeProjectId)
  const currentCwd = useStore($currentCwd)
  const selectedSessionId = useStore($focusedStoredSessionId)
  const sessions = useStore($sessions)
  const [artifacts, setArtifacts] = useState<ArtifactRecord[] | null>(null)
  const [reload, setReload] = useState(0)

  const currentSession = useMemo(
    () =>
      selectedSessionId ? (sessions.find(session => sessionMatchesStoredId(session, selectedSessionId)) ?? null) : null,
    [selectedSessionId, sessions]
  )

  const projectId = useMemo(
    () =>
      preferredArtifactProjectId({
        activeProjectId,
        currentCwd,
        projectScope,
        projects: projectTree,
        selectedSessionId
      }),
    [activeProjectId, currentCwd, projectScope, projectTree, selectedSessionId]
  )

  const project = projectTree.find(candidate => candidate.id === projectId) ?? null

  const contextSessions = useMemo(() => {
    const byId = new Map<string, SessionInfo>()

    if (currentSession) {
      byId.set(currentSession.id, currentSession)
    }

    for (const session of (project ? artifactSessionsForProject(project) : []).slice(0, PANE_PROJECT_SESSIONS)) {
      byId.set(session.id, session)
    }

    return [...byId.values()]
  }, [currentSession, project])

  useEffect(() => {
    let active = true

    // Hidden panes stay mounted for instant reveal, but do no message-history
    // reads until the user explicitly opens this pane.
    if (!open) {
      return () => {
        active = false
      }
    }

    setArtifacts(null)
    void collectSessionArtifacts(contextSessions)
      .then(next => {
        if (active) {
          setArtifacts(next)
        }
      })
      .catch(err => {
        if (active) {
          notifyError(err, t.artifacts.failedLoad)
          setArtifacts([])
        }
      })

    return () => {
      active = false
    }
  }, [contextSessions, open, reload, t.artifacts.failedLoad])

  const currentArtifacts = useMemo(
    () => artifacts?.filter(artifact => currentSession && artifact.sessionId === currentSession.id) ?? [],
    [artifacts, currentSession]
  )

  const projectArtifacts = useMemo(
    () => artifacts?.filter(artifact => !currentSession || artifact.sessionId !== currentSession.id) ?? [],
    [artifacts, currentSession]
  )

  const viewAll = useCallback(() => {
    const params = projectId ? `?project=${encodeURIComponent(projectId)}` : ''

    navigate(`${ARTIFACTS_ROUTE}${params}`)
  }, [navigate, projectId])

  return (
    <div className="flex h-full min-h-0 flex-col bg-(--ui-sidebar-surface-background)">
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-(--ui-stroke-tertiary) px-2.5">
        <span className="min-w-0 flex-1 truncate text-xs font-medium">
          {project ? t.artifacts.projectPaneTitle(project.label) : t.artifacts.paneTitle}
        </span>
        <Button
          aria-label={t.artifacts.refresh}
          onClick={() => setReload(value => value + 1)}
          size="icon-xs"
          variant="ghost"
        >
          <RefreshCw />
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
        {artifacts === null ? (
          <div className="flex h-24 items-center justify-center gap-2 text-xs text-(--ui-text-tertiary)">
            <Loader2 className="size-3.5 animate-spin" />
            {t.artifacts.indexingContext}
          </div>
        ) : artifacts.length === 0 ? (
          <div className="px-3 py-8 text-center">
            <div className="text-xs font-medium">{t.artifacts.noArtifactsTitle}</div>
            <div className="mt-1 text-[0.6875rem] text-(--ui-text-tertiary)">{t.artifacts.noContextArtifactsDesc}</div>
          </div>
        ) : (
          <div className="space-y-3">
            {currentSession && (
              <ArtifactPaneSection
                artifacts={currentArtifacts}
                empty={t.artifacts.noConversationArtifacts}
                label={t.artifacts.thisConversation}
              />
            )}
            {project && (
              <ArtifactPaneSection
                artifacts={projectArtifacts}
                empty={t.artifacts.noRecentProjectArtifacts}
                label={t.artifacts.recentInProject}
              />
            )}
          </div>
        )}
      </div>

      <div className="shrink-0 border-t border-(--ui-stroke-tertiary) p-2">
        <Button className="w-full" onClick={viewAll} size="sm" variant="outline">
          {t.artifacts.viewAll}
        </Button>
      </div>
    </div>
  )
}

function ArtifactPaneSection({
  artifacts,
  empty,
  label
}: {
  artifacts: readonly ArtifactRecord[]
  empty: string
  label: string
}) {
  const { t } = useI18n()

  return (
    <section>
      <h3 className="mb-1 px-1 text-[0.625rem] font-medium uppercase tracking-[0.08em] text-(--ui-text-tertiary)">
        {label}
      </h3>
      {artifacts.length === 0 ? (
        <div className="px-2 py-2 text-[0.6875rem] text-(--ui-text-quaternary)">{empty}</div>
      ) : (
        <div className="space-y-px">
          {artifacts.slice(0, PANE_SECTION_ITEMS).map(artifact => {
            const Icon = artifact.kind === 'image' ? FileImage : artifact.kind === 'link' ? Link2 : FileText

            return (
              <button
                className={cn(
                  'flex h-8 w-full items-center gap-2 rounded-md px-2 text-left',
                  'hover:bg-(--ui-control-hover-background)'
                )}
                key={artifact.id}
                onClick={() =>
                  void openArtifactHref(artifact.href).catch(err => notifyError(err, t.artifacts.openFailed))
                }
                title={artifact.value}
                type="button"
              >
                <Icon className="size-3.5 shrink-0 text-(--ui-text-tertiary)" />
                <span className="min-w-0 flex-1 truncate text-xs">{artifact.label}</span>
              </button>
            )
          })}
        </div>
      )}
    </section>
  )
}
