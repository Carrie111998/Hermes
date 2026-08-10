import {
  buildWorkspaceContextResearchPrompt,
  type WorkspaceContextSource
} from '@hermes/shared/workspace-context'
import { useStore } from '@nanostores/react'
import { type FormEvent, type ReactNode, useEffect, useState } from 'react'
import { useNavigate } from 'react-router'

import { sessionRoute } from '@/app/routes'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ErrorState } from '@/components/ui/error-state'
import { Input } from '@/components/ui/input'
import { Loader } from '@/components/ui/loader'
import { sessionTitle } from '@/lib/chat-runtime'
import { normalizeSessionSource, sessionSourceLabel } from '@/lib/session-source'
import { cn } from '@/lib/utils'
import type { ComposerAttachment } from '@/store/composer'
import { notifyError } from '@/store/notifications'
import {
  $activeProjectId,
  $projects,
  $projectScope,
  $projectTree,
  ALL_PROJECTS,
  enterProject,
  fetchProjectSessions,
  openProjectCreate,
  refreshProjectTree,
  requestStartWorkSession,
  startIsolatedWorkSession
} from '@/store/projects'
import { $reviewFiles } from '@/store/review'
import { $cronSessions, $currentCwd, $messagingSessions, $sessions } from '@/store/session'
import { $focusedSessionState, $focusedStoredSessionId, $workingSessionIds } from '@/store/session-states'
import {
  $workspaceContextBindings,
  setProjectSlackChannelIds
} from '@/store/workspace-context'

import { selectProjectWorkspaceSessions } from './projection'
import { pickWorkspaceAttachments, type WorkspaceAttachmentKind } from './workspace-attachments'

type Maybe<T> = T | null

export interface WorkspaceProject {
  id: string
  label: string
  path: string
  repoCount: number
}

export interface WorkspaceSession {
  id: string
  title: string
  preview: string
  active: boolean
  busy: boolean
  source?: string | null
}

interface WorkspaceContentProps {
  assetCount?: number
  attachments?: ComposerAttachment[]
  branch: string
  changedFiles: number
  cwd: string
  notionConnected: boolean
  onCreateProject: () => void
  onOpenSession: (id: string) => void
  onPickAttachments?: (kind: WorkspaceAttachmentKind) => void
  onRemoveAttachment?: (id: string) => void
  onSearchContext?: (query: string, sources: WorkspaceContextSource[]) => void
  onSelectProject: (id: string) => void
  onStartTask: (draft: string) => void
  project: Maybe<WorkspaceProject>
  projects: WorkspaceProject[]
  sessions: WorkspaceSession[]
  slackChannelIds?: string[]
  sessionsStatus?: 'error' | 'loading' | 'ready'
  taskStarting?: boolean
  onRetrySessions?: () => void
  onSaveSlackChannelIds?: (value: string) => void
}

const contextLabel = 'text-[0.64rem] font-semibold uppercase tracking-[0.16em] text-(--ui-text-tertiary)'
const panelBorder = 'border-(--ui-stroke-secondary)'
const ACTIVITY_SOURCE_ORDER = ['slack', 'cli', 'cron', 'subagent']
const EMPTY_SLACK_CHANNEL_IDS: string[] = []

function activitySourceText(source: string) {
  return sessionSourceLabel(source) || source
}

function StatusDot({ tone = 'muted' }: { tone?: 'accent' | 'muted' | 'success' | 'warning' }) {
  return <span aria-hidden="true" className={cn('size-1.5 rounded-full', {
    'bg-(--ui-accent)': tone === 'accent',
    'bg-(--ui-text-tertiary)': tone === 'muted',
    'bg-emerald-500': tone === 'success',
    'bg-amber-500': tone === 'warning'
  })} />
}

function ContextSection({ children, label }: { children: ReactNode; label: string }) {
  return (
    <section aria-label={label} className="border-b border-(--ui-stroke-secondary) px-4 py-4 last:border-b-0">
      <p className={contextLabel}>{label}</p>
      <div className="mt-3">{children}</div>
    </section>
  )
}

function WorkspaceMetric({ label, value, detail }: { detail: string; label: string; value: string }) {
  return (
    <div className={cn('min-w-0 border-r px-4 py-3 first:pl-0 last:border-r-0', panelBorder)}>
      <p className="truncate text-[0.65rem] text-(--ui-text-tertiary)">{label}</p>
      <p className="mt-1 text-lg font-semibold tracking-[-0.02em] text-(--ui-text-primary)">{value}</p>
      <p className="mt-0.5 truncate text-[0.66rem] text-(--ui-text-tertiary)">{detail}</p>
    </div>
  )
}

function WorkspaceTaskCard({ session, onOpen }: { onOpen: () => void; session: WorkspaceSession }) {
  return (
    <button
      className="group flex w-full items-start gap-3 border-b border-(--ui-stroke-secondary) px-1 py-3 text-left transition-colors hover:bg-(--ui-hover-overlay)"
      onClick={onOpen}
      type="button"
    >
      <span className="mt-1.5 flex size-5 shrink-0 items-center justify-center rounded-md bg-(--ui-sidebar-surface-background) text-(--ui-text-tertiary)">
        <Codicon name={session.busy ? 'loading~spin' : 'comment-discussion'} size="0.75rem" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2">
          <span className="truncate text-[0.78rem] font-medium text-(--ui-text-primary) group-hover:text-(--ui-accent)">
            {session.title}
          </span>
          {session.source && <span className="shrink-0 rounded-full bg-(--ui-sidebar-surface-background) px-1.5 py-0.5 text-[0.58rem] text-(--ui-text-tertiary)">{sessionSourceLabel(session.source)}</span>}
          {session.active && <span className="shrink-0 text-[0.62rem] text-(--ui-accent)">Focused</span>}
        </span>
        <span className="mt-1 block truncate text-[0.68rem] leading-5 text-(--ui-text-tertiary)">{session.preview}</span>
      </span>
      <Codicon className="mt-1 shrink-0 text-(--ui-text-tertiary) opacity-0 transition-opacity group-hover:opacity-100" name="chevron-right" size="0.8rem" />
    </button>
  )
}

export function ProjectWorkspaceContent({
  assetCount = 0,
  attachments = [],
  branch,
  changedFiles,
  cwd,
  notionConnected,
  onCreateProject,
  onOpenSession,
  onPickAttachments,
  onRemoveAttachment,
  onSearchContext,
  onSelectProject,
  onStartTask,
  onRetrySessions,
  onSaveSlackChannelIds,
  project,
  projects,
  sessions,
  slackChannelIds = EMPTY_SLACK_CHANNEL_IDS,
  sessionsStatus = 'ready',
  taskStarting = false
}: WorkspaceContentProps) {
  const navigate = useNavigate()
  const [draft, setDraft] = useState('')
  const [activitySource, setActivitySource] = useState('all')
  const [contextQuery, setContextQuery] = useState('')

  const [contextSources, setContextSources] = useState<WorkspaceContextSource[]>([
    'notion',
    ...(slackChannelIds.length ? ['slack' as const] : [])
  ])

  const [slackBindingDraft, setSlackBindingDraft] = useState(slackChannelIds.join(', '))

  useEffect(() => {
    setSlackBindingDraft(slackChannelIds.join(', '))

    if (!slackChannelIds.length) {
      setContextSources(current => current.filter(source => source !== 'slack'))
    }
  }, [slackChannelIds])

  const availableSources = Array.from(new Set(
    sessions
      .map(session => normalizeSessionSource(session.source))
      .filter((source): source is string => Boolean(source))
  )).sort((a, b) => {
    const aIndex = ACTIVITY_SOURCE_ORDER.indexOf(a)
    const bIndex = ACTIVITY_SOURCE_ORDER.indexOf(b)

    if (aIndex === -1 && bIndex === -1) {
      return a.localeCompare(b)
    }

    if (aIndex === -1) {
      return 1
    }

    if (bIndex === -1) {
      return -1
    }

    return aIndex - bIndex
  })

  const filteredSessions = activitySource === 'all'
    ? sessions
    : sessions.filter(session => normalizeSessionSource(session.source) === activitySource)

  const visibleSessions = filteredSessions

  const submitTask = (event?: FormEvent) => {
    event?.preventDefault()
    const value = draft.trim()

    if (!value || !canRunTask || taskStarting) {
      return
    }

    onStartTask(value)
    setDraft('')
  }

  const projectPath = project?.path || cwd || 'No workspace selected'
  const canRunTask = Boolean(project?.path || cwd)
  const branchLabel = branch || 'No branch detected'
  const taskLabel = sessions.length === 0 ? 'No sessions yet' : `${sessions.length} sessions available`

  const toggleContextSource = (source: WorkspaceContextSource) => {
    if (source === 'slack' && !slackChannelIds.length) {
      return
    }

    setContextSources(current =>
      current.includes(source) ? current.filter(item => item !== source) : [...current, source]
    )
  }

  const submitContextSearch = (event: FormEvent) => {
    event.preventDefault()

    const query = contextQuery.trim()

    if (!query || !contextSources.length || !canRunTask || !onSearchContext) {
      return
    }

    onSearchContext(query, contextSources)
    setContextQuery('')
  }

  return (
    <main className="flex h-full min-h-0 min-w-0 flex-col bg-(--ui-bg-chrome) text-(--ui-text-primary)" data-testid="project-workspace">
      <header className={cn('flex min-h-[3.75rem] shrink-0 items-center justify-between border-b px-6', panelBorder)}>
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-(--ui-accent)/12 text-(--ui-accent)">
            <Codicon name="layers" size="1rem" />
          </div>
          <div className="min-w-0">
            <p className="text-[0.64rem] font-semibold uppercase tracking-[0.17em] text-(--ui-text-tertiary)">Project workspace</p>
            <h1 className="truncate text-[0.94rem] font-semibold text-(--ui-text-primary)">{project?.label || 'Choose a project'}</h1>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <span className="hidden items-center gap-2 rounded-full border border-(--ui-stroke-secondary) px-2.5 py-1 text-[0.66rem] text-(--ui-text-secondary) sm:flex">
            <StatusDot tone="accent" />
            Auto routing
          </span>
          <span className="flex items-center gap-2 rounded-full bg-(--ui-sidebar-surface-background) px-2.5 py-1 text-[0.66rem] text-(--ui-text-secondary)">
            <Codicon name="sparkle" size="0.72rem" />
            Hermes
          </span>
        </div>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_19rem] max-[900px]:grid-cols-1">
        <section className="flex min-h-0 min-w-0 flex-col">
          <div className="shrink-0 px-6 pb-4 pt-6">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <p className={contextLabel}>Active project</p>
                <div className="mt-2 flex min-w-0 items-center gap-2">
                  <h2 className="truncate text-[1.35rem] font-semibold tracking-[-0.025em]">{project?.label || 'Start from a project'}</h2>
                  {project && <StatusDot tone="success" />}
                </div>
                <p className="mt-1 flex min-w-0 items-center gap-1.5 truncate font-mono text-[0.68rem] text-(--ui-text-tertiary)" title={projectPath}>
                  <Codicon name="folder" size="0.75rem" />
                  {projectPath}
                </p>
              </div>
              <Button className="shrink-0" onClick={() => navigate('/')} size="sm" variant="ghost">
                Open chat
                <Codicon name="arrow-right" size="0.78rem" />
              </Button>
            </div>

            {projects.length > 0 && (
              <div aria-label="Projects" className="mt-5 flex min-w-0 gap-1 overflow-x-auto border-b border-(--ui-stroke-secondary) pb-px">
                {projects.map(item => (
                  <button
                    aria-current={item.id === project?.id ? 'page' : undefined}
                    className={cn(
                      'flex max-w-[13rem] shrink-0 items-center gap-2 border-b-2 px-2 pb-2 text-[0.7rem] transition-colors',
                      item.id === project?.id
                        ? 'border-(--ui-accent) text-(--ui-text-primary)'
                        : 'border-transparent text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)'
                    )}
                    key={item.id}
                    onClick={() => onSelectProject(item.id)}
                    type="button"
                  >
                    <span className="size-1.5 shrink-0 rounded-full bg-(--ui-accent)/70" />
                    <span className="truncate">{item.label}</span>
                  </button>
                ))}
              </div>
            )}

            <div className="mt-5 grid grid-cols-3 border-y border-(--ui-stroke-secondary)">
              <WorkspaceMetric detail={taskLabel} label="Activity" value={`${sessions.length}`} />
              <WorkspaceMetric detail={branchLabel} label="Branch" value={branch ? 'Ready' : 'Detached'} />
              <WorkspaceMetric detail="Review surface" label="Changes" value={`${changedFiles}`} />
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-5">
            <div className="flex items-center justify-between gap-4 py-2">
              <div>
                <p className={contextLabel}>Recent work</p>
                <p className="mt-1 text-[0.72rem] text-(--ui-text-tertiary)">Pick up where the project left off.</p>
              </div>
              {sessions.length > 0 && <span className="shrink-0 text-[0.65rem] text-(--ui-text-tertiary)">Live session data</span>}
            </div>

            {sessions.length > 0 && (
              <div aria-label="Activity source" className="flex min-w-0 gap-1 overflow-x-auto pb-1">
                {[
                  { id: 'all', label: 'All activity' },
                  ...availableSources.map(source => ({ id: source, label: activitySourceText(source) }))
                ].map(filter => (
                  <button
                    aria-pressed={activitySource === filter.id}
                    className={cn(
                      'shrink-0 rounded-md border px-2 py-1 text-[0.64rem] transition-colors',
                      activitySource === filter.id
                        ? 'border-(--ui-accent)/40 bg-(--ui-accent)/10 text-(--ui-text-primary)'
                        : 'border-transparent text-(--ui-text-tertiary) hover:border-(--ui-stroke-secondary) hover:text-(--ui-text-secondary)'
                    )}
                    key={filter.id}
                    onClick={() => setActivitySource(filter.id)}
                    type="button"
                  >
                    {filter.label}
                  </button>
                ))}
              </div>
            )}

            {visibleSessions.length > 0 ? (
              <div className="mt-2">
                {visibleSessions.map(session => <WorkspaceTaskCard key={session.id} onOpen={() => onOpenSession(session.id)} session={session} />)}
              </div>
            ) : sessionsStatus === 'loading' && project ? (
              <div className="grid min-h-40 place-items-center" data-testid="workspace-sessions-loading">
                <Loader label="Loading project conversations" type="lemniscate-bloom" />
              </div>
            ) : sessionsStatus === 'error' && project ? (
              <ErrorState
                className="mx-auto min-h-40 max-w-sm place-content-center"
                description="Hermes could not load this project's conversations. Existing project files were not changed."
                title="Project conversations unavailable"
              >
                {onRetrySessions && <Button onClick={onRetrySessions} size="sm" variant="secondary">Retry</Button>}
              </ErrorState>
            ) : (
              <div className="mt-3 rounded-xl border border-dashed border-(--ui-stroke-secondary) px-5 py-8 text-center">
                <div className="mx-auto flex size-9 items-center justify-center rounded-lg bg-(--ui-sidebar-surface-background) text-(--ui-text-tertiary)">
                  <Codicon name="comment-discussion" size="1rem" />
                </div>
                <p className="mt-3 text-[0.78rem] font-medium text-(--ui-text-secondary)">
                  {sessions.length > 0 ? `No ${activitySourceText(activitySource)} sessions in recent activity.` : project ? 'No work has started in this project.' : 'Choose a project before starting work.'}
                </p>
                <p className="mx-auto mt-1 max-w-[20rem] text-[0.68rem] leading-5 text-(--ui-text-tertiary)">
                  {sessions.length > 0
                    ? 'Choose All activity or another source to see a different slice of the same session history.'
                    : project
                      ? 'Use the composer below to give Hermes a task with the project context attached.'
                      : 'Create a workspace to attach its repository, Notion decisions, and future task history.'}
                </p>
                {!project && (
                  <Button className="mt-4" onClick={onCreateProject} size="sm">
                    <Codicon name="add" size="0.78rem" />
                    Create project
                  </Button>
                )}
              </div>
            )}
          </div>

          <form className="shrink-0 border-t border-(--ui-stroke-secondary) px-6 py-4" onSubmit={submitTask}>
            <div className="rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-sidebar-surface-background) shadow-sm focus-within:border-(--ui-accent)/60">
              <textarea
                aria-label="Task request"
                className="block min-h-[3.6rem] w-full resize-none bg-transparent px-3.5 pt-3 text-[0.78rem] leading-5 text-(--ui-text-primary) outline-none placeholder:text-(--ui-text-tertiary)"
                onChange={event => setDraft(event.target.value)}
                onKeyDown={event => {
                  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                    submitTask(event)
                  }
                }}
                placeholder={canRunTask ? 'Ask Hermes to change this project…' : 'Create or select a project to start a task…'}
                value={draft}
              />
              {attachments.length > 0 && (
                <div className="flex flex-wrap gap-1.5 px-3 pt-2">
                  {attachments.map(attachment => (
                    <span
                      className="flex max-w-52 items-center gap-1.5 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-card) px-2 py-1 text-[0.65rem] text-(--ui-text-secondary)"
                      key={attachment.id}
                    >
                      {attachment.kind === 'image' && attachment.previewUrl ? (
                        <img alt="" className="size-5 rounded object-cover" src={attachment.previewUrl} />
                      ) : (
                        <Codicon name="file" size="0.7rem" />
                      )}
                      <span className="truncate">{attachment.label}</span>
                      {onRemoveAttachment && (
                        <button
                          aria-label={`Remove ${attachment.label}`}
                          className="shrink-0 text-(--ui-text-tertiary) hover:text-(--ui-text-primary)"
                          onClick={() => onRemoveAttachment(attachment.id)}
                          type="button"
                        >
                          <Codicon name="close" size="0.62rem" />
                        </button>
                      )}
                    </span>
                  ))}
                </div>
              )}
              <div className="flex items-center justify-between gap-3 px-2.5 pb-2.5 pt-1">
                <div className="flex items-center gap-1.5 text-[0.64rem] text-(--ui-text-tertiary)">
                  <Button
                    aria-label="Attach files"
                    disabled={!onPickAttachments || taskStarting}
                    onClick={() => onPickAttachments?.('file')}
                    size="xs"
                    type="button"
                    variant="ghost"
                  >
                    <Codicon name="attach" size="0.72rem" /> Files
                  </Button>
                  <Button
                    aria-label="Attach images"
                    disabled={!onPickAttachments || taskStarting}
                    onClick={() => onPickAttachments?.('image')}
                    size="xs"
                    type="button"
                    variant="ghost"
                  >
                    <Codicon name="image" size="0.72rem" /> Images
                  </Button>
                  <span className="hidden text-(--ui-text-tertiary) sm:inline">⌘ Enter to run</span>
                </div>
                <Button aria-label="Start task" disabled={!draft.trim() || !canRunTask || taskStarting} size="sm" type="submit">
                  {taskStarting ? 'Creating isolated workspace…' : 'Start task'}
                  <Codicon name="arrow-up" size="0.78rem" />
                </Button>
              </div>
            </div>
          </form>
        </section>

        <aside aria-label="Workspace context" className={cn('min-h-0 overflow-y-auto border-l bg-(--ui-sidebar-surface-background) max-[900px]:hidden', panelBorder)}>
          <ContextSection label="Knowledge">
            <form
              className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-card) p-3"
              onSubmit={submitContextSearch}
            >
              <div className="flex items-center gap-2 text-[0.74rem] font-medium text-(--ui-text-secondary)">
                <StatusDot tone={notionConnected ? 'success' : 'muted'} />
                {notionConnected ? 'Notion context available' : 'Knowledge connectors checked at run time'}
              </div>
              <p className="mt-2 text-[0.67rem] leading-5 text-(--ui-text-tertiary)">
                Search configured sources in a read-only chat. Findings include page URLs or message permalinks; writeback remains a draft until explicit approval.
              </p>
              <div className="mt-3 flex gap-1.5">
                {(['notion', 'slack'] as const).map(source => {
                  const selected = contextSources.includes(source)

                  return (
                    <Button
                      aria-label={`Include ${source === 'notion' ? 'Notion' : 'Slack'} source`}
                      aria-pressed={selected}
                      disabled={source === 'slack' && !slackChannelIds.length}
                      key={source}
                      onClick={() => toggleContextSource(source)}
                      size="xs"
                      type="button"
                      variant={selected ? 'secondary' : 'ghost'}
                    >
                      {source === 'notion' ? 'Notion' : 'Slack'}
                    </Button>
                  )
                })}
              </div>
              <div className="mt-2 grid gap-1.5">
                <Input
                  aria-label="Project Slack channel IDs"
                  onChange={event => setSlackBindingDraft(event.target.value)}
                  placeholder="Project Slack channel IDs, e.g. C0123ABC"
                  size="sm"
                  value={slackBindingDraft}
                />
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[0.6rem] text-(--ui-text-tertiary)">
                    Slack searches are limited to these channels; DMs are never searched.
                  </span>
                  <Button
                    disabled={!project || !onSaveSlackChannelIds}
                    onClick={() => onSaveSlackChannelIds?.(slackBindingDraft)}
                    size="xs"
                    type="button"
                    variant="ghost"
                  >
                    Save channels
                  </Button>
                </div>
              </div>
              <Input
                aria-label="Project context query"
                className="mt-2"
                onChange={event => setContextQuery(event.target.value)}
                placeholder="Search prior decisions, feedback, or research…"
                size="sm"
                value={contextQuery}
              />
              <Button
                className="mt-3 h-7 text-[0.66rem]"
                disabled={!contextQuery.trim() || !contextSources.length || !canRunTask || !onSearchContext}
                size="sm"
                type="submit"
              >
                {project && cwd ? 'Search project context' : 'Select a project to search'}
              </Button>
            </form>
          </ContextSection>

          <ContextSection label="Repository">
            <div className="space-y-3">
              <div className="flex items-start gap-2.5">
                <Codicon className="mt-0.5 text-(--ui-text-tertiary)" name="source-control" size="0.85rem" />
                <div className="min-w-0">
                  <p className="text-[0.72rem] font-medium text-(--ui-text-secondary)">{branchLabel}</p>
                  <p className="mt-1 truncate font-mono text-[0.64rem] text-(--ui-text-tertiary)" title={cwd}>{cwd || 'No repository workspace selected'}</p>
                </div>
              </div>
              <div className="flex items-center justify-between border-t border-(--ui-stroke-secondary) pt-3 text-[0.67rem]">
                <span className="text-(--ui-text-tertiary)">Review surface</span>
                <span className="font-medium text-(--ui-text-secondary)">{changedFiles === 0 ? 'Clean' : `${changedFiles} changed files`}</span>
              </div>
            </div>
          </ContextSection>

          <ContextSection label="Assets">
            <div className="flex items-start gap-2.5">
              <Codicon className="mt-0.5 text-(--ui-text-tertiary)" name="image" size="0.85rem" />
              <div>
                <p className="text-[0.72rem] font-medium text-(--ui-text-secondary)">{assetCount ? `${assetCount} generated assets` : 'No generated assets yet'}</p>
                <p className="mt-1 text-[0.67rem] leading-5 text-(--ui-text-tertiary)">Image versions and approvals will collect here.</p>
              </div>
            </div>
          </ContextSection>

          <ContextSection label="Routing">
            <div className="space-y-2.5 text-[0.68rem]">
              <div className="flex items-center justify-between"><span className="text-(--ui-text-tertiary)">Current mode</span><span className="font-medium text-(--ui-text-secondary)">Auto</span></div>
              <div className="flex items-center justify-between"><span className="text-(--ui-text-tertiary)">Selection</span><span className="font-medium text-(--ui-text-secondary)">Before each run</span></div>
              <div className="flex items-center justify-between"><span className="text-(--ui-text-tertiary)">Details</span><span className="font-medium text-(--ui-text-secondary)">In conversation</span></div>
            </div>
          </ContextSection>
        </aside>
      </div>
    </main>
  )
}

export function ProjectWorkspaceView() {
  const navigate = useNavigate()
  const projects = useStore($projects)
  const activeProjectId = useStore($activeProjectId)
  const projectScope = useStore($projectScope)
  const projectTree = useStore($projectTree)
  const currentCwd = useStore($currentCwd)
  const sessions = useStore($sessions)
  const cronSessions = useStore($cronSessions)
  const messagingSessions = useStore($messagingSessions)
  const focusedState = useStore($focusedSessionState)
  const focusedStoredSessionId = useStore($focusedStoredSessionId)
  const workingSessionIds = useStore($workingSessionIds)
  const workspaceContextBindings = useStore($workspaceContextBindings)
  const reviewFileCount = useStore($reviewFiles).length
  const [hydratedProject, setHydratedProject] = useState<Awaited<ReturnType<typeof fetchProjectSessions>>>(null)
  const [sessionsStatus, setSessionsStatus] = useState<'error' | 'loading' | 'ready'>('ready')
  const [retryGeneration, setRetryGeneration] = useState(0)
  const [taskStarting, setTaskStarting] = useState(false)
  const [workspaceAttachments, setWorkspaceAttachments] = useState<ComposerAttachment[]>([])

  useEffect(() => {
    void refreshProjectTree()
  }, [])

  const treeProjects: WorkspaceProject[] = projectTree
    .filter(project => !project.archived && !project.isNoProject)
    .map(project => ({
      id: project.id,
      label: project.label,
      path: project.path || project.repos.find(repo => repo.path)?.path || '',
      repoCount: project.repos.length
    }))

  const fallbackProjects: WorkspaceProject[] = projects
    .filter(project => !project.archived)
    .map(project => ({
      id: project.id,
      label: project.name,
      path: project.primary_path || project.folders[0]?.path || '',
      repoCount: project.folders.length
    }))

  const workspaceProjects = treeProjects.length ? treeProjects : fallbackProjects
  const scopedProjectId = projectScope !== ALL_PROJECTS ? projectScope : activeProjectId
  const activeProject = workspaceProjects.find(project => project.id === scopedProjectId) || workspaceProjects[0] || null
  const selectedProjectId = activeProject?.id ?? null

  const slackChannelIds = selectedProjectId
    ? workspaceContextBindings[selectedProjectId] ?? []
    : []

  useEffect(() => {
    if (!selectedProjectId) {
      setHydratedProject(null)
      setSessionsStatus('ready')

      return
    }

    let cancelled = false
    setHydratedProject(null)
    setSessionsStatus('loading')

    void fetchProjectSessions(selectedProjectId).then(project => {
      if (cancelled) {
        return
      }

      setHydratedProject(project)
      setSessionsStatus(project ? 'ready' : 'error')
    })

    return () => {
      cancelled = true
    }
  }, [selectedProjectId, retryGeneration])

  const activitySessionMap = new Map(
    [...sessions, ...messagingSessions, ...cronSessions].map(session => [session.id, session] as const)
  )

  const projectSessions = selectProjectWorkspaceSessions({
    allSessions: [...activitySessionMap.values()],
    hydratedProject,
    projectId: selectedProjectId,
    projects
  })

  const workspaceSessions: WorkspaceSession[] = projectSessions.map(session => ({
    id: session.id,
    title: sessionTitle(session),
    preview: session.preview || 'No session preview available yet.',
    source: session.source,
    active: session.id === focusedStoredSessionId,
    busy: workingSessionIds.includes(session.id)
  }))

  const focusedInProject = workspaceSessions.some(session => session.active)
  const cwd = activeProject?.path || (focusedInProject ? focusedState?.cwd : '') || currentCwd
  const branch = focusedInProject ? focusedState?.branch || '' : ''

  return (
    <ProjectWorkspaceContent
      assetCount={workspaceAttachments.filter(attachment => attachment.kind === 'image').length}
      attachments={workspaceAttachments}
      branch={branch}
      changedFiles={focusedInProject ? reviewFileCount : 0}
      cwd={cwd}
      notionConnected
      onCreateProject={openProjectCreate}
      onOpenSession={id => navigate(sessionRoute(id))}
      onPickAttachments={kind => {
        const path = activeProject?.path || cwd

        if (!path) {
          return
        }

        void pickWorkspaceAttachments({ cwd: path, kind })
          .then(picked => {
            setWorkspaceAttachments(current => [
              ...new Map([...current, ...picked].map(attachment => [attachment.id, attachment])).values()
            ])
          })
          .catch(error => notifyError(error, kind === 'image' ? 'Attach images' : 'Attach files'))
      }}
      onRemoveAttachment={id =>
        setWorkspaceAttachments(current => current.filter(attachment => attachment.id !== id))
      }
      onRetrySessions={() => setRetryGeneration(generation => generation + 1)}
      onSaveSlackChannelIds={value => {
        if (!selectedProjectId) {
          return
        }

        try {
          setProjectSlackChannelIds(selectedProjectId, value.split(/[\s,]+/))
        } catch (error) {
          notifyError(error, 'Save Slack channels')
        }
      }}
      onSearchContext={(query, sources) => {
        const path = activeProject?.path || cwd

        if (activeProject && path) {
          const prompt = buildWorkspaceContextResearchPrompt({
            projectId: activeProject.id,
            projectLabel: activeProject.label,
            query,
            slackChannelIds,
            sources
          })

          requestStartWorkSession(path, prompt, { openTab: true })
          navigate('/')
        }
      }}
      onSelectProject={id => enterProject(id)}
      onStartTask={draft => {
        const path = activeProject?.path || cwd

        if (path && !taskStarting) {
          setTaskStarting(true)
          void startIsolatedWorkSession(path, draft, workspaceAttachments)
            .then(() => {
              setWorkspaceAttachments([])
              navigate('/')
            })
            .catch(error => notifyError(error, 'Create isolated workspace'))
            .finally(() => setTaskStarting(false))
        }
      }}
      project={activeProject}
      projects={workspaceProjects}
      sessions={workspaceSessions}
      sessionsStatus={sessionsStatus}
      slackChannelIds={slackChannelIds}
      taskStarting={taskStarting}
    />
  )
}
