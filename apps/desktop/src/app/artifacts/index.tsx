import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { ZoomableImage } from '@/components/chat/zoomable-image'
import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { CopyButton } from '@/components/ui/copy-button'
import {
  Pagination,
  PaginationButton,
  PaginationContent,
  PaginationEllipsis,
  PaginationItem,
  PaginationNext,
  PaginationPrevious
} from '@/components/ui/pagination'
import { RowButton } from '@/components/ui/row-button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tip } from '@/components/ui/tooltip'
import { getSessionMessages } from '@/hermes'
import { translateNow, type Translations, useI18n } from '@/i18n'
import { resolveBrandIcon } from '@/lib/brand-icon'
import {
  ExternalLink,
  ExternalLinkIcon,
  hostPathLabel,
  shortHostLabel,
  urlSlugTitleLabel,
  useLinkTitle
} from '@/lib/external-link'
import { ChevronDown, ChevronRight, FileImage, FileText, FolderOpen, Link2, Loader2, RefreshCw } from '@/lib/icons'
import { downloadGatewayMediaFile, isRemoteGateway } from '@/lib/media'
import { normalize } from '@/lib/text'
import { fmtDayTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import { $activeProjectId, $projectScope, $projectTree, fetchProjectSessions } from '@/store/projects'
import { $currentCwd, $selectedStoredSessionId } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import type { SidebarProjectTree } from '../chat/sidebar/projects/workspace-groups'
import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import { openSession } from '../open-session'
import { PageSearchShell } from '../page-search-shell'
import type { SetStatusbarItemGroup } from '../shell/statusbar-controls'

import {
  ALL_ARTIFACT_PROJECTS,
  ARTIFACT_FILTERS,
  type ArtifactFilter,
  artifactImageSrc,
  type ArtifactRecord,
  artifactSessionsForProject,
  collectArtifactsForSession,
  preferredArtifactProjectId
} from './artifact-utils'
import { openArtifactsPane } from './pane-state'

function formatArtifactTime(timestamp: number): string {
  return fmtDayTime.format(new Date(timestamp))
}

function pageRangeLabel(total: number, page: number, pageSize: number, a: Translations['artifacts']): string {
  if (total === 0) {
    return a.zero
  }

  const start = (page - 1) * pageSize + 1
  const end = Math.min(total, page * pageSize)

  return a.rangeOf(start, end, total)
}

function paginationItems(page: number, pageCount: number): Array<number | 'ellipsis'> {
  if (pageCount <= 7) {
    return Array.from({ length: pageCount }, (_, index) => index + 1)
  }

  const pages: Array<number | 'ellipsis'> = [1]
  const start = Math.max(2, page - 1)
  const end = Math.min(pageCount - 1, page + 1)

  if (start > 2) {
    pages.push('ellipsis')
  }

  for (let nextPage = start; nextPage <= end; nextPage += 1) {
    pages.push(nextPage)
  }

  if (end < pageCount - 1) {
    pages.push('ellipsis')
  }

  pages.push(pageCount)

  return pages
}

type CellCtx = {
  onOpen: (href: string) => void | Promise<void>
  onOpenChat: (sessionId: string) => void
}

interface ArtifactColumn {
  Cell: React.ComponentType<{ artifact: ArtifactRecord; ctx: CellCtx }>
  bodyClassName: string
  header: (filter: ArtifactFilter, a: Translations['artifacts']) => string
  id: 'location' | 'primary' | 'session'
  width: (filter: ArtifactFilter) => string
}

const itemsLabel = (f: ArtifactFilter, a: Translations['artifacts']) =>
  f === 'link' ? a.itemsLink : f === 'file' ? a.itemsFile : a.itemsGeneric

interface ArtifactsViewProps extends React.ComponentProps<'section'> {
  setStatusbarItemGroup?: SetStatusbarItemGroup
}

const PROJECT_SESSION_BATCH = 12

interface ProjectArtifactsState {
  artifacts: ArtifactRecord[] | null
  loadingMore: boolean
  loadedSessions: number
  sessions: SessionInfo[]
}

const EMPTY_PROJECT_ARTIFACTS: ProjectArtifactsState = {
  artifacts: null,
  loadingMore: false,
  loadedSessions: 0,
  sessions: []
}

function mergeArtifacts(current: ArtifactRecord[], incoming: ArtifactRecord[]): ArtifactRecord[] {
  const byId = new Map(current.map(artifact => [artifact.id, artifact]))

  for (const artifact of incoming) {
    byId.set(artifact.id, artifact)
  }

  return [...byId.values()].sort((left, right) => right.timestamp - left.timestamp)
}

async function artifactsFromSessions(sessions: readonly SessionInfo[]): Promise<ArtifactRecord[]> {
  const results = await Promise.allSettled(sessions.map(session => getSessionMessages(session.id, session.profile)))
  const artifacts: ArtifactRecord[] = []

  results.forEach((result, index) => {
    if (result.status === 'fulfilled') {
      artifacts.push(...collectArtifactsForSession(sessions[index], result.value.messages))
    }
  })

  return artifacts
}

/** Project-local progressive loader. It asks the backend for one authoritative
 * project membership tree, then reads message histories in bounded batches.
 * No global session list and no eager work for other projects. */
function useProjectArtifacts(project: null | SidebarProjectTree, batchSize = PROJECT_SESSION_BATCH) {
  const [state, setState] = useState<ProjectArtifactsState>(EMPTY_PROJECT_ARTIFACTS)
  const [reload, setReload] = useState(0)
  const generationRef = useRef(0)

  useEffect(() => {
    let active = true
    const generation = ++generationRef.current

    if (!project) {
      setState(EMPTY_PROJECT_ARTIFACTS)

      return () => {
        active = false
      }
    }

    setState(EMPTY_PROJECT_ARTIFACTS)
    void fetchProjectSessions(project.id)
      .then(async hydrated => {
        if (!active || generation !== generationRef.current) {
          return
        }

        const sessions = artifactSessionsForProject(hydrated ?? project)
        const first = sessions.slice(0, batchSize)
        const artifacts = await artifactsFromSessions(first)

        if (active && generation === generationRef.current) {
          setState({
            artifacts,
            loadingMore: false,
            loadedSessions: first.length,
            sessions
          })
        }
      })
      .catch(err => {
        if (active && generation === generationRef.current) {
          notifyError(err, translateNow('artifacts.failedLoad'))
          setState({ artifacts: [], loadingMore: false, loadedSessions: 0, sessions: [] })
        }
      })

    return () => {
      active = false
    }
  }, [batchSize, project, reload])

  const loadMore = useCallback(async () => {
    const next = state.sessions.slice(state.loadedSessions, state.loadedSessions + batchSize)

    if (!next.length || state.loadingMore) {
      return
    }

    setState(current => ({ ...current, loadingMore: true }))
    const generation = generationRef.current
    const incoming = await artifactsFromSessions(next)

    if (generation !== generationRef.current) {
      return
    }

    setState(current => ({
      ...current,
      artifacts: mergeArtifacts(current.artifacts ?? [], incoming),
      loadedSessions: Math.min(current.sessions.length, current.loadedSessions + next.length),
      loadingMore: false
    }))
  }, [batchSize, state.loadedSessions, state.loadingMore, state.sessions])

  return {
    ...state,
    hasMore: state.loadedSessions < state.sessions.length,
    loadMore,
    refresh: () => setReload(value => value + 1)
  }
}

export function ArtifactsView({ setStatusbarItemGroup: _setStatusbarItemGroup, ...props }: ArtifactsViewProps) {
  const { t } = useI18n()
  const a = t.artifacts
  const navigate = useNavigate()
  const location = useLocation()
  const projectTree = useStore($projectTree)
  const projectScope = useStore($projectScope)
  const activeProjectId = useStore($activeProjectId)
  const currentCwd = useStore($currentCwd)
  const selectedSessionId = useStore($selectedStoredSessionId)
  const [query, setQuery] = useState('')
  const [kindFilter, setKindFilter] = useRouteEnumParam('tab', ARTIFACT_FILTERS, 'all')
  const [failedImageIds, setFailedImageIds] = useState<Set<string>>(() => new Set())
  const [imagePage, setImagePage] = useState(1)
  const [filePage, setFilePage] = useState(1)

  const preferredProject = useMemo(
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

  const rawProject = useMemo(() => new URLSearchParams(location.search).get('project'), [location.search])

  const selectedProjectId =
    rawProject === ALL_ARTIFACT_PROJECTS || projectTree.some(project => project.id === rawProject)
      ? rawProject
      : preferredProject

  const selectedProject = projectTree.find(project => project.id === selectedProjectId) ?? null
  const projectArtifacts = useProjectArtifacts(selectedProject)
  const artifacts = projectArtifacts.artifacts

  const setSelectedProject = useCallback(
    (projectId: string) => {
      const params = new URLSearchParams(location.search)

      if (projectId === preferredProject) {
        params.delete('project')
      } else {
        params.set('project', projectId)
      }

      navigate(
        {
          hash: location.hash,
          pathname: location.pathname,
          search: params.size ? `?${params.toString()}` : ''
        },
        { replace: true }
      )
    },
    [location.hash, location.pathname, location.search, navigate, preferredProject]
  )

  useRefreshHotkey(projectArtifacts.refresh)

  useEffect(() => {
    setImagePage(1)
    setFilePage(1)
  }, [artifacts, kindFilter, query, selectedProjectId])

  const visibleArtifacts = useMemo(() => {
    if (!artifacts) {
      return []
    }

    const q = normalize(query)

    return artifacts.filter(artifact => {
      if (kindFilter !== 'all' && artifact.kind !== kindFilter) {
        return false
      }

      if (!q) {
        return true
      }

      return (
        artifact.label.toLowerCase().includes(q) ||
        artifact.value.toLowerCase().includes(q) ||
        artifact.sessionTitle.toLowerCase().includes(q)
      )
    })
  }, [artifacts, kindFilter, query])

  const visibleImageArtifacts = useMemo(
    () => visibleArtifacts.filter(artifact => artifact.kind === 'image'),
    [visibleArtifacts]
  )

  const visibleFileArtifacts = useMemo(
    () => visibleArtifacts.filter(artifact => artifact.kind !== 'image'),
    [visibleArtifacts]
  )

  const imagePageCount = Math.max(1, Math.ceil(visibleImageArtifacts.length / 24))
  const filePageCount = Math.max(1, Math.ceil(visibleFileArtifacts.length / 100))
  const currentImagePage = Math.min(imagePage, imagePageCount)
  const currentFilePage = Math.min(filePage, filePageCount)

  const pagedImageArtifacts = useMemo(
    () => visibleImageArtifacts.slice((currentImagePage - 1) * 24, currentImagePage * 24),
    [currentImagePage, visibleImageArtifacts]
  )

  const pagedFileArtifacts = useMemo(
    () => visibleFileArtifacts.slice((currentFilePage - 1) * 100, currentFilePage * 100),
    [currentFilePage, visibleFileArtifacts]
  )

  // Rotating placeholder nudges from real data — search matches file paths and
  // session titles, not just labels; show it.
  const searchHints = useMemo(() => {
    if (!artifacts?.length) {
      return undefined
    }

    const extensions = [
      ...new Set(artifacts.map(artifact => /\.(\w{2,4})$/.exec(artifact.value)?.[1]?.toLowerCase()).filter(Boolean))
    ].slice(0, 3) as string[]

    const titles = [...new Set(artifacts.map(artifact => artifact.sessionTitle).filter(Boolean))].slice(0, 2)

    const hints = [
      ...extensions.map(ext => t.common.tryHint(`.${ext}`)),
      ...titles.map(title => t.common.tryHint(title))
    ]

    return hints.length > 0 ? hints : undefined
  }, [artifacts, t])

  const counts = useMemo(() => {
    const all = artifacts || []

    return {
      all: all.length,
      image: all.filter(artifact => artifact.kind === 'image').length,
      file: all.filter(artifact => artifact.kind === 'file').length,
      link: all.filter(artifact => artifact.kind === 'link').length
    }
  }, [artifacts])

  const allProjects = selectedProjectId === ALL_ARTIFACT_PROJECTS
  const refreshing = !allProjects && artifacts === null

  const openArtifact = useCallback(
    async (href: string) => {
      try {
        // A gateway-local file resolves to file:// in remote mode (the file
        // lives on the gateway, not this disk). Opening that locally fails —
        // and an OAuth remote connection has no query token to build a download
        // URL. Fetch the bytes over the authenticated fs bridge instead.
        if (isRemoteGateway() && /^file:/i.test(href)) {
          await downloadGatewayMediaFile(href)

          return
        }

        if (window.hermesDesktop?.openExternal) {
          await window.hermesDesktop.openExternal(href)
        } else {
          window.open(href, '_blank', 'noopener,noreferrer')
        }
      } catch (err) {
        notifyError(err, a.openFailed)
      }
    },
    [a]
  )

  const markImageFailed = useCallback((id: string) => {
    setFailedImageIds(current => {
      if (current.has(id)) {
        return current
      }

      return new Set(current).add(id)
    })
  }, [])

  // Stable ctx: recreating it (or its onOpenChat closure) every render made
  // every artifact cell re-render whenever the page did — and a link cell's
  // async title fetch re-rendered the page repeatedly. openArtifact is already
  // a useCallback; navigate is stable, so onOpenChat can be too.
  const openChat = useCallback((sessionId: string) => openSession(sessionId, navigate), [navigate])
  const cellCtx: CellCtx = useMemo(() => ({ onOpen: openArtifact, onOpenChat: openChat }), [openArtifact, openChat])

  return (
    <PageSearchShell
      {...props}
      activeTab={kindFilter}
      onSearchChange={setQuery}
      onTabChange={id => setKindFilter(id as typeof kindFilter)}
      searchHidden={!allProjects && counts.all === 0}
      searchHints={searchHints}
      searchPlaceholder={a.search}
      searchTrailingAction={
        <Tip label={refreshing ? a.refreshing : a.refresh}>
          <Button
            aria-label={refreshing ? a.refreshing : a.refresh}
            className="text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground"
            disabled={refreshing}
            onClick={projectArtifacts.refresh}
            size="icon-titlebar"
            variant="ghost"
          >
            {refreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
          </Button>
        </Tip>
      }
      searchValue={query}
      tabs={[
        { id: 'all', label: a.tabAll, meta: artifacts ? counts.all : null },
        { id: 'image', label: a.tabImages, meta: artifacts ? counts.image : null },
        { id: 'file', label: a.tabFiles, meta: artifacts ? counts.file : null },
        { id: 'link', label: a.tabLinks, meta: artifacts ? counts.link : null }
      ]}
    >
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex h-11 shrink-0 items-center gap-2 border-b border-(--ui-stroke-tertiary) px-3">
          <span className="shrink-0 text-xs font-medium text-(--ui-text-secondary)">{a.projectLibrary}</span>
          <Select onValueChange={setSelectedProject} value={selectedProjectId ?? ''}>
            <SelectTrigger className="h-7 min-w-0 max-w-64 flex-1 rounded-md text-xs">
              <SelectValue placeholder={a.noProjects} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_ARTIFACT_PROJECTS}>{a.allProjects}</SelectItem>
              {projectTree.map(project => (
                <SelectItem key={project.id} value={project.id}>
                  {project.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button onClick={openArtifactsPane} size="sm" type="button" variant="ghost">
            <FileText className="size-3.5" />
            {a.openSidebar}
          </Button>
        </div>

        {projectTree.length === 0 ? (
          <div className="grid min-h-0 flex-1 place-items-center px-6 text-center">
            <div>
              <div className="text-sm font-medium">{a.noProjects}</div>
              <div className="mt-1 text-xs text-muted-foreground">{a.noProjectsDesc}</div>
            </div>
          </div>
        ) : allProjects ? (
          <AllProjectsArtifacts
            filter={kindFilter}
            onOpenArtifact={openArtifact}
            onOpenChat={sessionId => openSession(sessionId, navigate)}
            onOpenProject={setSelectedProject}
            projects={projectTree}
            query={query}
          />
        ) : !artifacts ? (
          <PageLoader label={a.indexingProject(selectedProject?.label ?? '')} />
        ) : visibleArtifacts.length === 0 ? (
          <div className="grid min-h-0 flex-1 place-items-center px-6 text-center">
            <div>
              <div className="text-sm font-medium">{a.noArtifactsTitle}</div>
              <div className="mt-1 text-xs text-muted-foreground">{a.noArtifactsDesc}</div>
            </div>
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto [scrollbar-gutter:stable]">
            <div className="flex flex-col gap-3 px-3 pb-3">
              {visibleImageArtifacts.length > 0 && (
                <section className="flex flex-col">
                  <div className="sticky top-0 z-10 -mx-3 flex h-7 items-center gap-3 overflow-x-auto bg-background px-3">
                    <ArtifactsPagination
                      className="ml-auto justify-end px-0"
                      itemLabel={a.itemsImage}
                      onPageChange={setImagePage}
                      page={currentImagePage}
                      pageSize={24}
                      total={visibleImageArtifacts.length}
                    />
                  </div>
                  <div className="grid grid-cols-[repeat(auto-fill,minmax(11rem,1fr))] items-start gap-2 pt-1.5">
                    {pagedImageArtifacts.map(artifact => (
                      <ArtifactImageCard
                        artifact={artifact}
                        failedImage={failedImageIds.has(artifact.id)}
                        key={artifact.id}
                        onImageError={markImageFailed}
                        onOpenChat={sessionId => openSession(sessionId, navigate)}
                      />
                    ))}
                  </div>
                </section>
              )}

              {visibleFileArtifacts.length > 0 && (
                <section className="flex flex-col">
                  <div className="sticky top-0 z-10 -mx-3 flex h-7 items-center gap-3 overflow-x-auto bg-background px-3">
                    <ArtifactsPagination
                      className="ml-auto justify-end px-0"
                      itemLabel={itemsLabel(kindFilter, a)}
                      onPageChange={setFilePage}
                      page={currentFilePage}
                      pageSize={100}
                      total={visibleFileArtifacts.length}
                    />
                  </div>
                  <div className="overflow-x-auto rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background)">
                    <ArtifactTable artifacts={pagedFileArtifacts} ctx={cellCtx} filter={kindFilter} />
                  </div>
                </section>
              )}

              {projectArtifacts.hasMore && (
                <Button
                  className="self-center"
                  disabled={projectArtifacts.loadingMore}
                  onClick={() => void projectArtifacts.loadMore()}
                  size="sm"
                  variant="outline"
                >
                  {projectArtifacts.loadingMore && <Loader2 className="animate-spin" />}
                  {a.loadMoreSessions(projectArtifacts.loadedSessions, projectArtifacts.sessions.length)}
                </Button>
              )}
            </div>
          </div>
        )}
      </div>
    </PageSearchShell>
  )
}

function AllProjectsArtifacts({
  filter,
  onOpenArtifact,
  onOpenChat,
  onOpenProject,
  projects,
  query
}: {
  filter: ArtifactFilter
  onOpenArtifact: (href: string) => void | Promise<void>
  onOpenChat: (sessionId: string) => void
  onOpenProject: (projectId: string) => void
  projects: readonly SidebarProjectTree[]
  query: string
}) {
  const { t } = useI18n()
  const [visibleProjects, setVisibleProjects] = useState(20)

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2 [scrollbar-gutter:stable]">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-1.5">
        <p className="px-1 pb-1 text-xs text-(--ui-text-tertiary)">{t.artifacts.allProjectsHint}</p>
        {projects.slice(0, visibleProjects).map(project => (
          <ProjectArtifactGroup
            filter={filter}
            key={project.id}
            onOpenArtifact={onOpenArtifact}
            onOpenChat={onOpenChat}
            onOpenProject={onOpenProject}
            project={project}
            query={query}
          />
        ))}
        {visibleProjects < projects.length && (
          <Button
            className="mx-auto mt-1"
            onClick={() => setVisibleProjects(count => count + 20)}
            size="sm"
            variant="outline"
          >
            {t.sidebar.loadMore}
          </Button>
        )}
      </div>
    </div>
  )
}

function ProjectArtifactGroup({
  filter,
  onOpenArtifact,
  onOpenChat,
  onOpenProject,
  project,
  query
}: {
  filter: ArtifactFilter
  onOpenArtifact: (href: string) => void | Promise<void>
  onOpenChat: (sessionId: string) => void
  onOpenProject: (projectId: string) => void
  project: SidebarProjectTree
  query: string
}) {
  const { t } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const data = useProjectArtifacts(expanded ? project : null, 8)

  const visible = useMemo(() => {
    const q = normalize(query)

    return (data.artifacts ?? []).filter(artifact => {
      if (filter !== 'all' && artifact.kind !== filter) {
        return false
      }

      return (
        !q ||
        artifact.label.toLowerCase().includes(q) ||
        artifact.value.toLowerCase().includes(q) ||
        artifact.sessionTitle.toLowerCase().includes(q)
      )
    })
  }, [data.artifacts, filter, query])

  return (
    <section className="overflow-hidden rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background)">
      <div className="flex min-h-10 items-center gap-2 px-2">
        <Button
          aria-expanded={expanded}
          className="min-w-0 flex-1 justify-start px-1.5"
          onClick={() => setExpanded(value => !value)}
          size="sm"
          variant="ghost"
        >
          {expanded ? <ChevronDown className="size-3.5 shrink-0" /> : <ChevronRight className="size-3.5 shrink-0" />}
          <span className="truncate font-medium">{project.label}</span>
          <span className="ml-auto shrink-0 text-[0.6875rem] font-normal text-(--ui-text-tertiary)">
            {t.artifacts.sessionCount(project.sessionCount)}
          </span>
        </Button>
        <Button onClick={() => onOpenProject(project.id)} size="xs" variant="textStrong">
          {t.artifacts.openProject}
        </Button>
      </div>

      {expanded && (
        <div className="border-t border-(--ui-stroke-tertiary) px-2 py-1.5">
          {!data.artifacts ? (
            <div className="flex h-16 items-center justify-center gap-2 text-xs text-(--ui-text-tertiary)">
              <Loader2 className="size-3.5 animate-spin" />
              {t.artifacts.indexingProject(project.label)}
            </div>
          ) : visible.length === 0 ? (
            <div className="px-2 py-5 text-center text-xs text-(--ui-text-tertiary)">
              {t.artifacts.noArtifactsInProject}
            </div>
          ) : (
            <div className="divide-y divide-(--ui-stroke-tertiary)">
              {visible.slice(0, 20).map(artifact => {
                const Icon = artifact.kind === 'image' ? FileImage : artifact.kind === 'link' ? Link2 : FileText

                return (
                  <div className="flex items-center gap-1 hover:bg-(--ui-control-hover-background)" key={artifact.id}>
                    <button
                      className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left"
                      onClick={() => void onOpenArtifact(artifact.href)}
                      type="button"
                    >
                      <Icon className="size-3.5 shrink-0 text-(--ui-text-tertiary)" />
                      <span className="min-w-0 flex-1 truncate text-xs">{artifact.label}</span>
                    </button>
                    <span className="max-w-40 truncate text-[0.6875rem] text-(--ui-text-tertiary)">
                      {artifact.sessionTitle}
                    </span>
                    <Button
                      className="mr-1 shrink-0"
                      onClick={() => onOpenChat(artifact.sessionId)}
                      size="xs"
                      variant="textStrong"
                    >
                      {t.artifacts.chat}
                    </Button>
                  </div>
                )
              })}
            </div>
          )}

          {data.hasMore && (
            <Button
              className="mx-auto mt-1 flex"
              disabled={data.loadingMore}
              onClick={() => void data.loadMore()}
              size="xs"
              variant="ghost"
            >
              {data.loadingMore && <Loader2 className="animate-spin" />}
              {t.artifacts.loadMoreSessions(data.loadedSessions, data.sessions.length)}
            </Button>
          )}
        </div>
      )}
    </section>
  )
}

interface ArtifactsPaginationProps {
  className?: string
  itemLabel: string
  onPageChange: (page: number) => void
  page: number
  pageSize: number
  total: number
}

function ArtifactsPagination({ className, itemLabel, onPageChange, page, pageSize, total }: ArtifactsPaginationProps) {
  const { t } = useI18n()
  const a = t.artifacts
  const pageCount = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className={cn('flex h-6 items-center justify-between gap-2 px-1', className)}>
      <div className="shrink-0 text-[0.62rem] text-muted-foreground">
        {pageRangeLabel(total, page, pageSize, a)} {itemLabel}
      </div>
      {pageCount > 1 && (
        <Pagination className="mx-0 w-auto min-w-0 justify-end">
          <PaginationContent className="gap-0.5">
            <PaginationItem>
              <PaginationPrevious disabled={page <= 1} onClick={() => onPageChange(Math.max(1, page - 1))} />
            </PaginationItem>
            {paginationItems(page, pageCount).map((item, index) => (
              <PaginationItem key={`${item}-${index}`}>
                {item === 'ellipsis' ? (
                  <PaginationEllipsis />
                ) : (
                  <PaginationButton
                    aria-label={a.goToPage(itemLabel, item)}
                    isActive={page === item}
                    onClick={() => onPageChange(item)}
                  >
                    {item}
                  </PaginationButton>
                )}
              </PaginationItem>
            ))}
            <PaginationItem>
              <PaginationNext
                disabled={page >= pageCount}
                onClick={() => onPageChange(Math.min(pageCount, page + 1))}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      )}
    </div>
  )
}

interface ArtifactImageCardProps {
  artifact: ArtifactRecord
  failedImage: boolean
  onImageError: (id: string) => void
  onOpenChat: (sessionId: string) => void
}

function ArtifactImageCard({ artifact, failedImage, onImageError, onOpenChat }: ArtifactImageCardProps) {
  const { t } = useI18n()
  const a = t.artifacts
  const kindLabel = artifact.kind === 'image' ? a.kindImage : artifact.kind === 'file' ? a.kindFile : a.kindLink
  const [src, setSrc] = useState('')

  useEffect(() => {
    let active = true

    setSrc('')
    void artifactImageSrc(artifact.value, artifact.href)
      .then(nextSrc => {
        if (active) {
          setSrc(nextSrc)
        }
      })
      .catch(() => {
        if (active) {
          onImageError(artifact.id)
        }
      })

    return () => {
      active = false
    }
  }, [artifact.href, artifact.id, artifact.value, onImageError])

  return (
    <article className="group/artifact overflow-hidden rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background)">
      <div
        className={cn(
          'relative flex h-40 w-full items-center justify-center overflow-hidden border-b border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) p-1.5',
          failedImage && 'cursor-default'
        )}
      >
        {!failedImage && src && (
          <ZoomableImage
            alt={artifact.label}
            className="max-h-40 max-w-full cursor-zoom-in rounded-md object-contain"
            containerClassName="max-h-full"
            decoding="async"
            loading="lazy"
            onError={() => onImageError(artifact.id)}
            slot="artifact-media"
            src={src}
          />
        )}
      </div>

      <div className="space-y-1.5 p-2">
        <div className="min-w-0">
          <div className="mb-0.5 flex items-center gap-1 text-[0.625rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)">
            <FileImage className="size-3" />
            {kindLabel}
          </div>
          <div className="truncate text-[length:var(--conversation-caption-font-size)] font-medium">
            {artifact.label}
          </div>
          <div className="mt-0.5 truncate text-[0.625rem] text-(--ui-text-tertiary)">{artifact.value}</div>
        </div>

        <div className="truncate text-[0.625rem] text-(--ui-text-tertiary)">
          {artifact.sessionTitle} · {formatArtifactTime(artifact.timestamp)}
        </div>

        <div className="flex flex-wrap gap-1.5">
          <Button onClick={() => onOpenChat(artifact.sessionId)} size="xs" type="button" variant="textStrong">
            <FolderOpen className="size-3" />
            {a.chat}
          </Button>
        </div>
      </div>
    </article>
  )
}

// Single click target for any row cell. External URLs render as <ExternalLink>;
// local actions render as <button>. Padding lives here, NOT on the <td>, so
// the entire cell area is hoverable and clickable in both branches.
function ArtifactCellAction({
  children,
  href,
  onClick,
  title
}: {
  children: React.ReactNode
  href?: string
  onClick?: () => void
  title?: string
}) {
  if (href) {
    return (
      <ExternalLink
        className="flex h-full w-full min-w-0 items-center gap-2 px-2.5 py-1.5 text-left text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) font-normal text-(--ui-text-secondary) no-underline underline-offset-4 decoration-current/20 transition-colors hover:text-foreground hover:underline"
        href={href}
        showExternalIcon={false}
        title={title}
      >
        {children}
      </ExternalLink>
    )
  }

  return (
    <RowButton
      className="flex h-full w-full min-w-0 items-center gap-2 px-2.5 py-1.5 text-left text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) font-normal text-(--ui-text-secondary) no-underline underline-offset-4 decoration-current/20 transition-colors hover:text-foreground hover:underline"
      onClick={onClick}
    >
      {children}
    </RowButton>
  )
}

const PrimaryCell = memo(function PrimaryCell({ artifact, ctx }: { artifact: ArtifactRecord; ctx: CellCtx }) {
  const isLink = artifact.kind === 'link'
  const brand = isLink ? resolveBrandIcon(shortHostLabel(artifact.href)) : null
  const Icon = brand ?? (isLink ? Link2 : FileText)
  const fetchedTitle = useLinkTitle(isLink ? artifact.href : null)
  const label = isLink ? fetchedTitle || urlSlugTitleLabel(artifact.href) : artifact.label

  return (
    <ArtifactCellAction
      href={isLink ? artifact.href : undefined}
      onClick={isLink ? undefined : () => void ctx.onOpen(artifact.href)}
      title={label}
    >
      <span className="mt-0.5 grid size-6 shrink-0 place-items-center self-start rounded-md bg-(--ui-bg-tertiary) text-(--ui-text-tertiary)">
        <Icon className="size-3.5" />
      </span>
      <span className={cn('min-w-0 flex-1', isLink ? 'wrap-anywhere' : 'truncate')}>
        {label}
        {isLink && <ExternalLinkIcon />}
      </span>
    </ArtifactCellAction>
  )
})

const LocationCell = memo(function LocationCell({ artifact }: { artifact: ArtifactRecord; ctx: CellCtx }) {
  const { t } = useI18n()
  const isLink = artifact.kind === 'link'
  const value = isLink ? hostPathLabel(artifact.value) : artifact.value
  const copyLabel = isLink ? t.artifacts.copyUrl : t.artifacts.copyPath

  return (
    <div className="group/location flex min-w-0 items-center gap-1.5">
      <Tip label={artifact.value}>
        <div
          className={cn(
            'min-w-0 flex-1 truncate text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)',
            isLink ? 'font-normal' : 'font-mono'
          )}
        >
          {value}
        </div>
      </Tip>
      <CopyButton
        appearance="icon"
        buttonSize="icon-xs"
        className="shrink-0 text-muted-foreground opacity-0 transition-opacity hover:text-foreground focus-visible:opacity-100 group-hover/location:opacity-100"
        iconClassName="size-3.5"
        label={copyLabel}
        text={artifact.value}
        title={copyLabel}
      />
    </div>
  )
})

const SessionCell = memo(function SessionCell({ artifact, ctx }: { artifact: ArtifactRecord; ctx: CellCtx }) {
  return (
    <ArtifactCellAction onClick={() => ctx.onOpenChat(artifact.sessionId)} title={artifact.sessionTitle}>
      <span className="flex min-w-0 flex-col">
        <span className="truncate">{artifact.sessionTitle}</span>
        <span className="truncate text-[0.6875rem] font-normal text-(--ui-text-tertiary)">
          {formatArtifactTime(artifact.timestamp)}
        </span>
      </span>
    </ArtifactCellAction>
  )
})

const ARTIFACT_COLUMNS: readonly ArtifactColumn[] = [
  {
    Cell: PrimaryCell,
    bodyClassName: 'p-0',
    header: (filter, a) =>
      filter === 'link' ? a.colTitleLink : filter === 'file' ? a.colTitleFile : a.colTitleDefault,
    id: 'primary',
    width: filter => (filter === 'link' ? 'w-[50%]' : 'w-[35%]')
  },
  {
    Cell: LocationCell,
    bodyClassName: 'px-2.5 py-1.5',
    header: (filter, a) =>
      filter === 'link' ? a.colLocationLink : filter === 'file' ? a.colLocationFile : a.colLocationDefault,
    id: 'location',
    width: filter => (filter === 'link' ? 'w-[30%]' : 'w-[41%]')
  },
  {
    Cell: SessionCell,
    bodyClassName: 'p-0',
    header: (_filter, a) => a.colSession,
    id: 'session',
    width: filter => (filter === 'link' ? 'w-[20%]' : 'w-[24%]')
  }
]

function ArtifactTable({
  artifacts,
  ctx,
  filter
}: {
  artifacts: readonly ArtifactRecord[]
  ctx: CellCtx
  filter: ArtifactFilter
}) {
  const { t } = useI18n()

  return (
    <table className="w-full min-w-176 table-fixed text-left text-[length:var(--conversation-caption-font-size)]">
      <thead className="border-b border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) text-[0.625rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)">
        <tr>
          {ARTIFACT_COLUMNS.map(col => (
            <th className={cn(col.width(filter), 'px-2.5 py-1.5 font-medium')} key={col.id}>
              {col.header(filter, t.artifacts)}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {artifacts.map(artifact => (
          <tr className="group/artifact" key={artifact.id}>
            {ARTIFACT_COLUMNS.map(col => {
              const Cell = col.Cell

              return (
                <td className={cn('align-middle', col.bodyClassName)} key={col.id}>
                  <Cell artifact={artifact} ctx={ctx} />
                </td>
              )
            })}
          </tr>
        ))}
      </tbody>
    </table>
  )
}
