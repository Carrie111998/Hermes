import { host, type PluginRestOptions, type PluginStorage, useQuery, useValue } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect, useRef, useState } from 'react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import {
  $boardSlug,
  $profileDescriptionWriteOwner,
  bindApi,
  boardKey,
  BOARDS_KEY,
  createTask,
  fetchBoard,
  fetchLog,
  fetchOrchestration,
  fetchProfiles,
  fetchTask,
  logKey,
  ORCHESTRATION_KEY,
  orchestrationKey,
  patchTask,
  profileQueryOptions,
  PROFILES_KEY,
  profilesKey,
  saveOrchestration,
  taskKey
} from './api'
import { Card, KanbanBoardPage, NewTaskDialog, resolveNewTaskAssignee, SelectionBar } from './board'
import { TaskDrawer } from './drawer'
import { OrchestrationPanel } from './orchestration'
import type { KanbanProfile, OrchestrationSettings } from './types'
import { useDefaultAssignee, useOrchestration } from './ui'

vi.mock('./i18n', () => ({
  columnHelp: (_messages: unknown, name: string) => name,
  columnLabel: (_messages: unknown, name: string) => name,
  lockedReason: (_messages: unknown, name: string) => name,
  useKanban: () => ({
    assignee: 'Assignee',
    addComment: 'Add a comment',
    archive: 'Archive',
    archiveTask: 'Archive task',
    attachments: (count: number) => `Attachments · ${count}`,
    auto: 'Auto',
    autoDescribeFailed: 'Could not generate a profile description',
    autoDecompose: 'Auto-decompose',
    boardLoadError: 'Could not load this board',
    boardDefaultSuffix: ' (board default)',
    blockedByMachinePolicy: 'blocked by machine policy',
    bulkFailed: (failed: number, total: number, error: string) => `${failed}/${total}: ${error}`,
    cancel: 'Cancel',
    cancelEdit: 'Cancel edit',
    close: 'Close',
    clearSelection: 'Clear selection',
    col: {
      archived: { help: '', label: 'Archived' },
      blocked: { help: '', label: 'Blocked' },
      done: { help: '', label: 'Done' },
      ready: { help: '', label: 'Ready' },
      review: { help: '', label: 'Review' },
      running: { help: '', label: 'Running' },
      scheduled: { help: '', label: 'Scheduled' },
      todo: { help: '', label: 'Todo' },
      triage: { help: '', label: 'Triage' }
    },
    comment: 'Comment',
    comments: (count: number) => `Comments · ${count}`,
    commentsHelp: 'Comments help',
    complexity: { L: 'Large', M: 'Medium', S: 'Small' },
    couldNotEstimate: 'Could not estimate',
    createTask: 'Create task',
    creating: 'Creating…',
    copiedId: (id: string) => `Copied ${id}`,
    copiedTitle: 'Copied title',
    copyTaskId: 'Copy task id',
    copyTitle: 'Copy title',
    defaultOption: (name: string) => `Default (${name})`,
    defaultResolved: (name: string) => `Default (effective: ${name})`,
    defaultAssignee: 'Default assignee',
    defaultParen: '(default)',
    delete: 'Delete',
    deleteTask: 'Delete task',
    deselect: 'Deselect',
    description: 'Description',
    descPlaceholder: 'Description',
    editDescription: 'Edit description',
    estimate: 'Estimate',
    estimateEffort: 'Estimate effort',
    estimateTip: 'Estimate task',
    estimateTipLong: 'Estimate task effort',
    estimating: 'Estimating…',
    goalMode: 'Goal mode',
    inheritMachinePolicy: 'Inherit machine policy',
    makesModelCall: 'makes a model call',
    model: 'Model',
    modelClear: 'Clear model',
    modelHint: 'Optional model override',
    modelInherit: 'Inherit profile model',
    newTask: 'New task',
    newTaskIn: (name: string) => `New task in ${name}`,
    noAttachments: 'No attachments',
    noDescription: 'No description',
    noParent: 'No parent',
    noTasks: 'No tasks on this board',
    noWorkersCanRun: 'No workers can run on this board.',
    nSelected: (count: number) => `${count} selected`,
    none: 'None',
    orchestrationLoadError: 'Could not load orchestration settings',
    orchestratorProfile: 'Orchestrator profile',
    open: 'Open',
    parent: 'Parent',
    parkedOption: 'Parked',
    priority: 'Priority',
    profileDescriptions: 'Profile descriptions',
    profileDescriptionsHint: 'Used by the decomposer to route work.',
    profileGoodAt: 'What this profile is good at',
    profilesAllowedBoard: 'Profiles allowed on this board',
    refused: 'refused',
    configuredBlocked: (name: string) => `${name} (configured, blocked)`,
    effectiveFallback: (name: string) => `Effective fallback: ${name}`,
    reEstimate: 'Re-estimate',
    retry: 'Retry',
    roughEstimate: 'Rough estimate',
    save: 'Save',
    select: (modifier: string) => `Select (${modifier})`,
    skills: 'Skills',
    skillsPlaceholder: 'Comma-separated skills',
    taskActions: 'Task actions',
    taskLoadError: 'Could not load this task',
    taskPolicyLoadError: 'Could not load assignment policy',
    title: 'Kanban',
    titlePlaceholder: 'Task title',
    titlePlaceholderTriage: 'Triage title',
    tokUnit: 'tokens',
    unassigned: 'Unassigned',
    unassignAction: 'Unassign',
    uploadAttachment: 'Upload attachment',
    workspace: 'Workspace',
    workspaceInherit: 'Inherit board workspace',
    workspaceInheritDir: (dir: string) => `Inherit ${dir}`,
    workspaceInheritGeneric: 'Inherit the board workspace',
    workspaceOverride: 'Workspace override',
    assign: 'Assign',
    moveToShort: 'Move to'
  })
}))

type Rest = Parameters<typeof bindApi>[0]

interface PolicyBoard {
  allowed: null | string[]
  effective: string[]
}

const MACHINE_ALLOWED = ['alpha', 'beta']

function profile(name: string, board: PolicyBoard): KanbanProfile {
  const machineAllowed = MACHINE_ALLOWED.includes(name)

  return {
    name,
    is_default: false,
    description: `${name} description`,
    description_auto: false,
    machine_allowed: machineAllowed,
    board_selected: board.allowed === null || board.allowed.includes(name),
    effective_allowed: board.effective.includes(name)
  }
}

function orchestration(boardName: string, board: PolicyBoard): OrchestrationSettings {
  const resolvedOrchestrator = board.effective.includes('alpha') ? 'alpha' : (board.effective[0] ?? null)
  const resolvedDefault = board.effective.includes('beta') ? 'beta' : (board.effective[0] ?? null)

  return {
    board: boardName,
    orchestrator_profile: 'alpha',
    default_assignee: 'beta',
    auto_decompose: true,
    auto_promote_children: true,
    resolved_orchestrator_profile: resolvedOrchestrator,
    resolved_default_assignee: resolvedDefault,
    active_profile: 'alpha',
    board_allowed_profiles: board.allowed,
    effective_allowed_profiles: [...board.effective]
  }
}

function createPolicyRest(putGate?: Promise<void>, boardsGate?: Promise<void>, policyGate?: Promise<void>) {
  const boards: Record<string, PolicyBoard> = {
    'board-a': { allowed: ['alpha', 'blocked'], effective: ['alpha'] },
    'board-b': { allowed: null, effective: ['alpha', 'beta'] },
    'board/a': { allowed: ['alpha'], effective: ['alpha'] },
    empty: { allowed: [], effective: [] }
  }

  const requests: Array<{ body: unknown; method: string; path: string }> = []

  const rest = vi.fn(async (path: string, opts?: PluginRestOptions) => {
    const url = new URL(path, 'http://kanban.test')
    const boardName = url.searchParams.get('board') ?? 'board-a'
    const board = boards[boardName]
    const method = opts?.method ?? 'GET'

    if (!board) {
      throw new Error(`unknown board ${boardName}`)
    }

    requests.push({ body: opts?.body, method, path })

    if (url.pathname === '/profiles' && method === 'GET') {
      await policyGate

      return { profiles: ['alpha', 'beta', 'blocked'].map(name => profile(name, board)) }
    }

    if (url.pathname === '/boards' && method === 'GET') {
      await boardsGate

      return {
        boards: Object.keys(boards).map(slug => ({
          default_workspace_kind: slug === 'board-b' ? 'worktree' : 'scratch',
          slug
        })),
        current: 'board-a'
      }
    }

    if (url.pathname === '/orchestration' && method === 'GET') {
      await policyGate

      return orchestration(boardName, board)
    }

    if (url.pathname === '/orchestration' && method === 'PUT') {
      await putGate
      const patch = (opts?.body ?? {}) as Record<string, unknown>

      if (Object.hasOwn(patch, 'allowed_profiles')) {
        const allowed = patch.allowed_profiles as null | string[]
        board.allowed = allowed === null ? null : [...allowed]
        board.effective =
          allowed === null ? [...MACHINE_ALLOWED] : allowed.filter(name => MACHINE_ALLOWED.includes(name))
      }

      return orchestration(boardName, board)
    }

    if (/^\/profiles\/[^/]+$/.test(url.pathname) && method === 'PATCH') {
      return {}
    }

    if (/^\/profiles\/[^/]+\/describe-auto$/.test(url.pathname) && method === 'POST') {
      return { description: 'Generated profile description', ok: true }
    }

    if (url.pathname === '/tasks' && method === 'POST') {
      const body = opts?.body as { title?: string }

      return { task: { id: 't-created', status: 'triage', title: String(body.title) } }
    }

    throw new Error(`unexpected request ${method} ${path}`)
  }) as unknown as Rest

  return { boards, requests, rest }
}

function allowedProfiles(body: unknown): unknown {
  return body && typeof body === 'object' ? (body as { allowed_profiles?: unknown }).allowed_profiles : undefined
}

const clients: QueryClient[] = []
let disposeApi: null | (() => void) = null

function bind(rest: Rest) {
  const storage: PluginStorage = {
    get: <T,>(_key: string, fallback: T) => fallback,
    remove: vi.fn(),
    set: vi.fn()
  }

  const socket = vi.fn(() => vi.fn())

  disposeApi = bindApi(rest, storage, socket)
}

function renderPanel(rest: Rest, board = 'board-a') {
  bind(rest)
  $boardSlug.set(board)

  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
  clients.push(client)

  render(
    <QueryClientProvider client={client}>
      <OrchestrationPanel />
    </QueryClientProvider>
  )

  return client
}

function FallbackProbe() {
  const settings = useOrchestration()

  return (
    <p data-testid="fallback">
      {settings ? `${settings.board}:${settings.resolved_default_assignee ?? 'none'}` : 'loading'}
    </p>
  )
}

function DefaultProbe() {
  const assignee = useDefaultAssignee()

  return <p data-testid="default-assignee">{assignee || 'none'}</p>
}

function BoardProbe() {
  const slug = useValue($boardSlug)

  const { data } = useQuery({
    queryFn: () => fetchBoard(slug, false),
    queryKey: boardKey(slug, false)
  })

  return <p data-testid="board">{data ? `${slug}:${data.now}` : 'loading'}</p>
}

function drawerDetail(board: string, taskId: string) {
  return {
    attachments: [],
    comments: [],
    events: [],
    links: { children: [], parents: [] },
    runs: [],
    task: { assignee: 'alpha', id: taskId, status: 'done', title: `${board}:${taskId}` }
  }
}

function cacheDrawerTask(client: QueryClient, board: string, taskId: string) {
  client.setQueryData(taskKey(board, taskId), drawerDetail(board, taskId))
  client.setQueryData(logKey(board, taskId), { content: '', exists: false, size_bytes: 0, truncated: false })
  client.setQueryData(profilesKey(board), { profiles: [] })
  client.setQueryData(orchestrationKey(board), orchestration(board, { allowed: null, effective: [] }))
}

function DrawerHarness({ id, onClose }: { id: string; onClose: () => void }) {
  const [openId, setOpenId] = useState<null | string>(id)

  useEffect(() => setOpenId(id), [id])

  return (
    <TaskDrawer
      columns={['done']}
      id={openId}
      onClose={() => {
        onClose()
        setOpenId(null)
      }}
      onOpen={setOpenId}
    />
  )
}

function ReopenDrawerHarness() {
  const [open, setOpen] = useState(false)

  return (
    <>
      <button onClick={() => setOpen(true)} type="button">
        Open drawer
      </button>
      <TaskDrawer columns={['done']} id={open ? 'task-a' : null} onClose={() => setOpen(false)} onOpen={vi.fn()} />
    </>
  )
}

function ControlledNewTaskHarness({ onClose }: { onClose: (board: string) => void }) {
  const [target, setTarget] = useState<null | string>('triage')

  return (
    <NewTaskDialog
      onClose={board => {
        onClose(board)
        setTarget(null)
      }}
      parents={[]}
      target={target}
    />
  )
}

function BulkSelectionHarness() {
  const [selection, setSelection] = useState({ generation: 0, ids: new Set(['old-task']) })
  const current = useRef(selection)
  current.current = selection

  const replace = (ids: string[]) =>
    setSelection(previous => ({ generation: previous.generation + 1, ids: new Set(ids) }))

  return (
    <>
      <button onClick={() => replace(['new-task'])} type="button">
        Replace selection
      </button>
      <output data-testid="selection">{[...selection.ids].join(',')}</output>
      <SelectionBar
        columns={['done']}
        generation={selection.generation}
        isCurrent={(board, generation) => $boardSlug.get() === board && current.current.generation === generation}
        onClear={() => replace([])}
        onDone={(board, generation, failed) => {
          if ($boardSlug.get() === board && current.current.generation === generation) {
            replace(failed)
          }
        }}
        selected={selection.ids}
      />
    </>
  )
}

function createDeferredDrawerRest() {
  const requests: Array<{ body: unknown; method: string; path: string }> = []
  let releaseWrite!: () => void

  const writeGate = new Promise<void>(resolve => {
    releaseWrite = resolve
  })

  const rest = vi.fn(async (path: string, opts?: PluginRestOptions) => {
    const url = new URL(path, 'http://kanban.test')
    const board = url.searchParams.get('board') ?? ''
    const method = opts?.method ?? 'GET'
    const attachmentMatch = /^\/tasks\/([^/]+)\/attachments$/.exec(url.pathname)
    const taskMatch = /^\/tasks\/([^/]+)$/.exec(url.pathname)
    requests.push({ body: opts?.body, method, path })

    if (taskMatch && method === 'GET') {
      return drawerDetail(board, decodeURIComponent(taskMatch[1]))
    }

    if (taskMatch && (method === 'PATCH' || method === 'DELETE')) {
      await writeGate

      return {}
    }

    if (attachmentMatch && method === 'POST') {
      await writeGate

      return {}
    }

    if (url.pathname === '/dispatch' && method === 'POST') {
      return {}
    }

    throw new Error(`unexpected request ${method} ${path}`)
  }) as unknown as Rest

  return { releaseWrite, requests, rest }
}

function createDeferredAssignmentRest() {
  const assignees = new Map<string, null | string>([
    ['empty:task-a', 'alpha'],
    ['board-b:task-b', 'alpha']
  ])

  const requests: Array<{ body: unknown; method: string; path: string }> = []
  let releaseAssignment!: () => void

  const assignmentGate = new Promise<void>(resolve => {
    releaseAssignment = resolve
  })

  const rest = vi.fn(async (path: string, opts?: PluginRestOptions) => {
    const url = new URL(path, 'http://kanban.test')
    const board = url.searchParams.get('board') ?? ''
    const method = opts?.method ?? 'GET'
    const logMatch = /^\/tasks\/([^/]+)\/log$/.exec(url.pathname)
    const reassignMatch = /^\/tasks\/([^/]+)\/reassign$/.exec(url.pathname)
    const taskMatch = /^\/tasks\/([^/]+)$/.exec(url.pathname)
    requests.push({ body: opts?.body, method, path })

    if (taskMatch && method === 'GET') {
      const taskId = decodeURIComponent(taskMatch[1])
      const detail = drawerDetail(board, taskId)
      const key = `${board}:${taskId}`

      return { ...detail, task: { ...detail.task, assignee: assignees.has(key) ? assignees.get(key) : 'alpha' } }
    }

    if (logMatch && method === 'GET') {
      return { content: '', exists: false, size_bytes: 0, truncated: false }
    }

    if (reassignMatch && method === 'POST') {
      await assignmentGate
      const taskId = decodeURIComponent(reassignMatch[1])
      const profile = (opts?.body as { profile: null | string }).profile
      assignees.set(`${board}:${taskId}`, profile)

      return { assignee: profile, ok: true, task_id: taskId }
    }

    if (url.pathname === '/dispatch' && method === 'POST') {
      return {}
    }

    throw new Error(`unexpected request ${method} ${path}`)
  }) as unknown as Rest

  return { releaseAssignment, requests, rest }
}

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

afterEach(() => {
  cleanup()
  disposeApi?.()
  disposeApi = null
  $boardSlug.set('')
  $profileDescriptionWriteOwner.set(null)
  clients.splice(0).forEach(client => client.clear())
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('board-scoped orchestration API', () => {
  it('scopes profile and orchestration GETs and PUTs to the selected board', async () => {
    const { requests, rest } = createPolicyRest()
    bind(rest)
    $boardSlug.set('board/a')

    await fetchProfiles('board/a')
    await fetchOrchestration('board/a')
    await saveOrchestration('board/a', { allowed_profiles: null })

    expect(requests.map(request => [request.method, request.path])).toEqual([
      ['GET', '/profiles?board=board%2Fa'],
      ['GET', '/orchestration?board=board%2Fa'],
      ['PUT', '/orchestration?board=board%2Fa']
    ])
  })

  it('keeps fallback reads in independent caches and refetches when the board changes', async () => {
    const { requests, rest } = createPolicyRest()
    bind(rest)
    $boardSlug.set('board-a')

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <FallbackProbe />
      </QueryClientProvider>
    )

    expect(await screen.findByText('board-a:alpha')).toBeTruthy()

    act(() => $boardSlug.set('board-b'))

    expect(await screen.findByText('board-b:beta')).toBeTruthy()
    expect(requests.filter(request => request.path === '/orchestration?board=board-a')).toHaveLength(1)
    expect(requests.filter(request => request.path === '/orchestration?board=board-b')).toHaveLength(1)
    expect(client.getQueryData<OrchestrationSettings>(orchestrationKey('board-a'))?.board).toBe('board-a')
    expect(client.getQueryData<OrchestrationSettings>(orchestrationKey('board-b'))?.board).toBe('board-b')
  })

  it('keeps assignment rosters in independent board caches even when the selected board differs', async () => {
    const { requests, rest } = createPolicyRest()
    bind(rest)
    $boardSlug.set('board-b')

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)

    const boardA = await client.fetchQuery(profileQueryOptions('board-a'))
    const boardB = await client.fetchQuery(profileQueryOptions('board-b'))

    expect(boardA.profiles.filter(profile => profile.effective_allowed).map(profile => profile.name)).toEqual(['alpha'])
    expect(boardB.profiles.filter(profile => profile.effective_allowed).map(profile => profile.name)).toEqual([
      'alpha',
      'beta'
    ])
    expect(requests.filter(request => request.path === '/profiles?board=board-a')).toHaveLength(1)
    expect(requests.filter(request => request.path === '/profiles?board=board-b')).toHaveLength(1)
    expect(client.getQueryData(profilesKey('board-a'))).toBe(boardA)
    expect(client.getQueryData(profilesKey('board-b'))).toBe(boardB)
  })

  it('refetches board data on A→B and pins delayed task writes and per-board nudges to their boards', async () => {
    const requests: Array<{ body: unknown; method: string; path: string }> = []
    let releaseCreate!: () => void

    const createGate = new Promise<void>(resolve => {
      releaseCreate = resolve
    })

    const rest = vi.fn(async (path: string, opts?: PluginRestOptions) => {
      const url = new URL(path, 'http://kanban.test')
      const board = url.searchParams.get('board') ?? ''
      const method = opts?.method ?? 'GET'
      requests.push({ body: opts?.body, method, path })

      if (url.pathname === '/board') {
        return { assignees: [], columns: [], latest_event_id: 0, now: board === 'board-a' ? 1 : 2, tenants: [] }
      }

      if (url.pathname === '/tasks/t-1' && method === 'GET') {
        return { attachments: [], comments: [], events: [], links: { children: [], parents: [] }, runs: [], task: {} }
      }

      if (url.pathname === '/tasks/t-1/log') {
        return { content: '', exists: false, size_bytes: 0, truncated: false }
      }

      if (url.pathname === '/tasks' && method === 'POST') {
        await createGate

        return { task: { id: 't-1', status: 'ready', title: 'Pinned task' } }
      }

      if ((url.pathname === '/tasks/t-1' && method === 'PATCH') || url.pathname === '/dispatch') {
        return {}
      }

      throw new Error(`unexpected request ${method} ${path}`)
    }) as unknown as Rest

    bind(rest)
    $boardSlug.set('board-a')

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <BoardProbe />
      </QueryClientProvider>
    )

    expect(await screen.findByText('board-a:1')).toBeTruthy()
    await fetchTask('board-a', 't-1')
    await fetchLog('board-a', 't-1')

    const creating = createTask('board-a', { title: 'Pinned task' })
    act(() => $boardSlug.set('board-b'))
    expect(await screen.findByText('board-b:2')).toBeTruthy()

    vi.useFakeTimers()
    releaseCreate()
    const { task } = await creating
    await patchTask('board-a', task!.id, { status: 'triage' })
    await patchTask('board-b', 't-1', { status: 'done' })
    await vi.advanceTimersByTimeAsync(400)

    expect(requests.filter(request => request.path === '/board?board=board-a')).toHaveLength(1)
    expect(requests.filter(request => request.path === '/board?board=board-b')).toHaveLength(1)
    expect(requests).toContainEqual({ body: { status: 'triage' }, method: 'PATCH', path: '/tasks/t-1?board=board-a' })
    expect(requests).toContainEqual({ body: { status: 'done' }, method: 'PATCH', path: '/tasks/t-1?board=board-b' })
    expect(requests.filter(request => request.path === '/dispatch?board=board-a')).toHaveLength(1)
    expect(requests.filter(request => request.path === '/dispatch?board=board-b')).toHaveLength(1)
  })
})

describe('task drawer action identity', () => {
  it('keeps the B drawer open when a deferred A archive completes', async () => {
    const { releaseWrite, requests, rest } = createDeferredDrawerRest()
    bind(rest)
    $boardSlug.set('board-a')

    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false, staleTime: Infinity } }
    })

    const onClose = vi.fn()
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    clients.push(client)
    cacheDrawerTask(client, 'board-a', 'task-a')
    cacheDrawerTask(client, 'board-b', 'task-b')

    const view = render(
      <QueryClientProvider client={client}>
        <DrawerHarness id="task-a" onClose={onClose} />
      </QueryClientProvider>
    )

    expect(await screen.findByText('board-a:task-a')).toBeTruthy()
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Task actions' }), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse'
    })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Archive task' }))
    await waitFor(() =>
      expect(requests).toContainEqual({
        body: { status: 'archived' },
        method: 'PATCH',
        path: '/tasks/task-a?board=board-a'
      })
    )

    act(() => $boardSlug.set('board-b'))
    view.rerender(
      <QueryClientProvider client={client}>
        <DrawerHarness id="task-b" onClose={onClose} />
      </QueryClientProvider>
    )
    expect(await screen.findByText('board-b:task-b')).toBeTruthy()

    releaseWrite()
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: taskKey('board-a', 'task-a') }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kanban', 'board', 'board-a'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: BOARDS_KEY })

    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByText('board-b:task-b')).toBeTruthy()
  })

  it('unassigns on a deny-all board with exact ownership and authoritative cache reconciliation', async () => {
    const { releaseAssignment, requests, rest } = createDeferredAssignmentRest()
    bind(rest)
    $boardSlug.set('empty')

    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false, staleTime: Infinity } }
    })

    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const denyAll = { allowed: [], effective: [] }
    clients.push(client)
    cacheDrawerTask(client, 'empty', 'task-a')
    client.setQueryData(profilesKey('empty'), {
      profiles: ['alpha', 'beta', 'blocked'].map(name => profile(name, denyAll))
    })
    cacheDrawerTask(client, 'board-b', 'task-b')

    const renderDrawer = (taskId: string) => (
      <QueryClientProvider client={client}>
        <TaskDrawer columns={['done']} id={taskId} onClose={vi.fn()} onOpen={vi.fn()} />
      </QueryClientProvider>
    )

    const view = render(renderDrawer('task-a'))
    expect(await screen.findByRole('dialog', { name: 'empty:task-a' })).toBeTruthy()

    const assignee = screen.getByRole('button', { name: 'alpha' })
    fireEvent.keyDown(assignee, { key: 'Enter' })
    const unassign = await screen.findByRole('menuitem', { name: 'Unassign' })
    expect(screen.queryByRole('menuitem', { name: 'alpha' })).toBeNull()
    unassign.focus()
    fireEvent.keyDown(unassign, { key: 'Enter' })

    await waitFor(() =>
      expect(requests).toContainEqual({
        body: { profile: null, reclaim_first: true },
        method: 'POST',
        path: '/tasks/task-a/reassign?board=empty'
      })
    )
    await waitFor(() =>
      expect(
        client.getQueryData<{ task: { assignee?: null | string } }>(taskKey('empty', 'task-a'))?.task.assignee
      ).toBeNull()
    )
    expect(screen.getByRole('button', { name: 'Unassigned' }).hasAttribute('disabled')).toBe(true)

    act(() => $boardSlug.set('board-b'))
    view.rerender(renderDrawer('task-b'))
    expect(await screen.findByRole('dialog', { name: 'board-b:task-b' })).toBeTruthy()
    const boardBAssignee = screen.getByRole('button', { name: 'alpha' })
    expect(boardBAssignee.hasAttribute('disabled')).toBe(true)
    fireEvent.click(boardBAssignee)
    expect(requests.filter(request => request.path.includes('/reassign'))).toHaveLength(1)

    releaseAssignment()
    await waitFor(() => expect(boardBAssignee.hasAttribute('disabled')).toBe(false))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: taskKey('empty', 'task-a') })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kanban', 'board', 'empty'] })
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: taskKey('board-b', 'task-b') })
    expect(
      client.getQueryData<{ task: { assignee?: null | string } }>(taskKey('board-b', 'task-b'))?.task.assignee
    ).toBe('alpha')
    expect(screen.getByRole('button', { name: 'alpha' })).toBeTruthy()

    act(() => $boardSlug.set('empty'))
    view.rerender(renderDrawer('task-a'))
    expect(await screen.findByRole('dialog', { name: 'empty:task-a' })).toBeTruthy()
    await waitFor(() =>
      expect(requests.filter(request => request.path === '/tasks/task-a?board=empty')).toHaveLength(1)
    )
    expect(
      client.getQueryData<{ task: { assignee?: null | string } }>(taskKey('empty', 'task-a'))?.task.assignee
    ).toBeNull()

    const unassigned = screen.getByRole('button', { name: 'Unassigned' })
    expect(unassigned.hasAttribute('disabled')).toBe(false)
    fireEvent.keyDown(unassigned, { key: 'Enter' })
    expect((await screen.findByRole('menuitem', { name: 'Unassigned' })).getAttribute('aria-disabled')).toBe('true')
  })

  it('closes normally when a deferred delete completes for the current task identity', async () => {
    const { releaseWrite, requests, rest } = createDeferredDrawerRest()
    bind(rest)
    $boardSlug.set('board-a')

    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false, staleTime: Infinity } }
    })

    const onClose = vi.fn()
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    clients.push(client)
    cacheDrawerTask(client, 'board-a', 'task-a')

    render(
      <QueryClientProvider client={client}>
        <DrawerHarness id="task-a" onClose={onClose} />
      </QueryClientProvider>
    )

    expect(await screen.findByText('board-a:task-a')).toBeTruthy()
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Task actions' }), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse'
    })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete task' }))
    await waitFor(() =>
      expect(requests).toContainEqual({ body: undefined, method: 'DELETE', path: '/tasks/task-a?board=board-a' })
    )

    releaseWrite()
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())

    expect(invalidate).toHaveBeenCalledWith({ queryKey: taskKey('board-a', 'task-a') })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kanban', 'board', 'board-a'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: BOARDS_KEY })
    expect(screen.queryByText('board-a:task-a')).toBeNull()
  })

  it('rejects an old archive completion after closing and reopening the same drawer identity', async () => {
    const { releaseWrite, requests, rest } = createDeferredDrawerRest()
    bind(rest)
    $boardSlug.set('board-a')

    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false, staleTime: Infinity } }
    })

    clients.push(client)
    cacheDrawerTask(client, 'board-a', 'task-a')
    render(
      <QueryClientProvider client={client}>
        <ReopenDrawerHarness />
      </QueryClientProvider>
    )

    const trigger = screen.getByRole('button', { name: 'Open drawer' })
    fireEvent.click(trigger)
    expect(await screen.findByRole('dialog', { name: 'board-a:task-a' })).toBeTruthy()

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Task actions' }), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse'
    })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Archive task' }))
    await waitFor(() =>
      expect(requests).toContainEqual({
        body: { status: 'archived' },
        method: 'PATCH',
        path: '/tasks/task-a?board=board-a'
      })
    )

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.click(trigger)
    expect(await screen.findByRole('dialog', { name: 'board-a:task-a' })).toBeTruthy()

    releaseWrite()
    await waitFor(() =>
      expect(requests.filter(request => request.path === '/tasks/task-a?board=board-a')).toHaveLength(1)
    )
    expect(screen.getByRole('dialog', { name: 'board-a:task-a' })).toBeTruthy()
  })

  it('owns modal focus, loops Tab in both directions, closes on Escape, and restores prior focus', async () => {
    const { rest } = createDeferredDrawerRest()
    bind(rest)
    $boardSlug.set('board-a')

    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false, staleTime: Infinity } }
    })

    clients.push(client)
    cacheDrawerTask(client, 'board-a', 'task-a')
    render(
      <QueryClientProvider client={client}>
        <ReopenDrawerHarness />
      </QueryClientProvider>
    )

    const trigger = screen.getByRole('button', { name: 'Open drawer' })
    trigger.focus()
    fireEvent.click(trigger)

    const dialog = await screen.findByRole('dialog', { name: 'board-a:task-a' })
    const close = screen.getByRole('button', { name: 'Close' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(window.document.activeElement).toBe(close)

    const focusable = [
      ...dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ]

    const first = focusable[0]
    const last = focusable.at(-1)!

    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(window.document.activeElement).toBe(first)

    first.focus()
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(window.document.activeElement).toBe(last)

    fireEvent.keyDown(last, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(window.document.activeElement).toBe(trigger)
  })

  it('loops focus past a hidden file input while its visible upload control is disabled', async () => {
    const { releaseWrite, requests, rest } = createDeferredDrawerRest()
    bind(rest)
    $boardSlug.set('board-a')

    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false, staleTime: Infinity } }
    })

    clients.push(client)
    cacheDrawerTask(client, 'board-a', 'task-a')
    render(
      <QueryClientProvider client={client}>
        <ReopenDrawerHarness />
      </QueryClientProvider>
    )

    fireEvent.click(screen.getByRole('button', { name: 'Open drawer' }))
    const dialog = await screen.findByRole('dialog', { name: 'board-a:task-a' })
    const first = screen.getByRole('button', { name: 'done' })
    const actualLast = screen.getByPlaceholderText('Add a comment')
    const uploadButton = screen.getByRole('button', { name: 'Upload attachment' })
    const hiddenInput = dialog.querySelector<HTMLInputElement>('input[type="file"]')!
    const hiddenFocus = vi.fn()
    hiddenInput.addEventListener('focus', hiddenFocus)

    fireEvent.change(hiddenInput, {
      target: { files: [new File(['attachment'], 'attachment.txt', { type: 'text/plain' })] }
    })
    await waitFor(() =>
      expect(requests).toContainEqual({
        body: undefined,
        method: 'POST',
        path: '/tasks/task-a/attachments?board=board-a'
      })
    )
    expect(uploadButton.hasAttribute('disabled')).toBe(true)

    actualLast.focus()
    fireEvent.keyDown(actualLast, { key: 'Tab' })
    expect(window.document.activeElement).toBe(first)

    first.focus()
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(window.document.activeElement).toBe(actualLast)
    expect(window.document.activeElement).not.toBe(hiddenInput)
    expect(hiddenFocus).not.toHaveBeenCalled()

    releaseWrite()
    await waitFor(() => expect(uploadButton.hasAttribute('disabled')).toBe(false))
  })
})

describe('terminal query recovery', () => {
  it('retries a terminal board load error against the same board', async () => {
    const base = createPolicyRest()
    const paths: string[] = []
    let failing = true

    const rest = vi.fn((path: string, opts?: PluginRestOptions) => {
      const url = new URL(path, 'http://kanban.test')

      if (url.pathname === '/board') {
        paths.push(path)

        if (failing) {
          return Promise.reject(new Error('board offline'))
        }

        return Promise.resolve({
          assignees: [],
          columns: [{ name: 'triage', tasks: [] }],
          latest_event_id: 0,
          now: 1,
          tenants: []
        })
      }

      return base.rest(path, opts)
    }) as unknown as Rest

    bind(rest)
    $boardSlug.set('board-a')
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <KanbanBoardPage />
      </QueryClientProvider>
    )

    expect(await screen.findByText('Could not load this board')).toBeTruthy()
    failing = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByText('No tasks on this board')).toBeTruthy()
    expect(paths).toEqual(['/board?board=board-a', '/board?board=board-a'])
  })

  it('retries a terminal drawer load error against the same task identity', async () => {
    const base = createPolicyRest()
    const paths: string[] = []
    let failing = true

    const rest = vi.fn((path: string, opts?: PluginRestOptions) => {
      const url = new URL(path, 'http://kanban.test')
      const method = opts?.method ?? 'GET'

      if (url.pathname === '/tasks/task-a' && method === 'GET') {
        paths.push(path)

        return failing ? Promise.reject(new Error('task offline')) : Promise.resolve(drawerDetail('board-a', 'task-a'))
      }

      if (url.pathname === '/tasks/task-a/log' && method === 'GET') {
        return Promise.resolve({ content: '', exists: false, size_bytes: 0, truncated: false })
      }

      return base.rest(path, opts)
    }) as unknown as Rest

    bind(rest)
    $boardSlug.set('board-a')
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <TaskDrawer columns={['done']} id="task-a" onClose={vi.fn()} onOpen={vi.fn()} />
      </QueryClientProvider>
    )

    expect(await screen.findByText('Could not load this task')).toBeTruthy()
    failing = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByText('board-a:task-a')).toBeTruthy()
    expect(paths).toEqual(['/tasks/task-a?board=board-a', '/tasks/task-a?board=board-a'])
  })
})

describe('task card keyboard access', () => {
  it('opens with Enter/Space and retains modifier-selection behavior', () => {
    $boardSlug.set('board-a')
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(orchestrationKey('board-a'), orchestration('board-a', { allowed: null, effective: ['alpha'] }))
    clients.push(client)
    const onOpen = vi.fn()
    const onToggleSelect = vi.fn()

    render(
      <QueryClientProvider client={client}>
        <Card
          columns={['done']}
          onDelete={vi.fn()}
          onMove={vi.fn()}
          onOpen={onOpen}
          onToggleSelect={onToggleSelect}
          selected={false}
          task={{ id: 'keyboard-task', status: 'done', title: 'Keyboard task' }}
        />
      </QueryClientProvider>
    )

    const card = screen.getByRole('button', { name: 'Keyboard task' })
    fireEvent.keyDown(card, { key: 'Enter' })
    fireEvent.keyDown(card, { key: ' ' })
    fireEvent.keyDown(card, { ctrlKey: true, key: 'Enter' })
    fireEvent.click(card, { ctrlKey: true })

    expect(onOpen).toHaveBeenCalledTimes(2)
    expect(onOpen).toHaveBeenNthCalledWith(1, 'keyboard-task')
    expect(onOpen).toHaveBeenNthCalledWith(2, 'keyboard-task')
    expect(onToggleSelect).toHaveBeenCalledTimes(2)
    expect(onToggleSelect).toHaveBeenCalledWith('keyboard-task')
  })
})

describe('new-task effective default resolution', () => {
  it('uses the resolved default rather than a blocked raw configured default, including null', async () => {
    const { rest } = createPolicyRest()
    bind(rest)
    $boardSlug.set('board-a')
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <DefaultProbe />
      </QueryClientProvider>
    )

    // board-a stores raw beta, but policy resolves it to alpha.
    expect(await screen.findByText('alpha')).toBeTruthy()

    act(() => $boardSlug.set('empty'))
    expect(await screen.findByText('none')).toBeTruthy()
  })

  it('never fabricates a default when the board has no effective worker', () => {
    const alpha = new Set(['alpha'])

    expect(resolveNewTaskAssignee('', null, alpha)).toBeUndefined()
    expect(resolveNewTaskAssignee('', undefined, alpha)).toBeUndefined()
    expect(resolveNewTaskAssignee('__parked__', 'alpha', alpha)).toBeUndefined()
    expect(resolveNewTaskAssignee('', 'alpha', alpha)).toBe('alpha')
    expect(resolveNewTaskAssignee('beta', 'alpha', alpha)).toBe('alpha')
    expect(resolveNewTaskAssignee('beta', null, new Set())).toBeUndefined()
  })

  it('keeps typed form input when late board metadata initializes a pristine workspace default', async () => {
    let releaseBoards!: () => void

    const boardsGate = new Promise<void>(resolve => {
      releaseBoards = resolve
    })

    const { rest } = createPolicyRest(undefined, boardsGate)
    bind(rest)
    $boardSlug.set('board-b')
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <NewTaskDialog onClose={vi.fn()} parents={[]} target="triage" />
      </QueryClientProvider>
    )

    const title = await screen.findByPlaceholderText('Triage title')
    fireEvent.change(title, { target: { value: 'Do not erase me' } })
    releaseBoards()

    const workspace = await screen.findByRole('combobox', { name: 'Workspace' })
    await waitFor(() => expect(workspace.textContent).toContain('worktree'))
    expect((title as HTMLInputElement).value).toBe('Do not erase me')
  })

  it('keeps create disabled until both orchestration and the effective profile roster load', async () => {
    let releasePolicy!: () => void

    const policyGate = new Promise<void>(resolve => {
      releasePolicy = resolve
    })

    const { rest } = createPolicyRest(undefined, undefined, policyGate)
    bind(rest)
    $boardSlug.set('board-b')
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <NewTaskDialog onClose={vi.fn()} parents={[]} target="triage" />
      </QueryClientProvider>
    )

    fireEvent.change(await screen.findByPlaceholderText('Triage title'), { target: { value: 'Wait for policy' } })
    const create = screen.getByRole('button', { name: 'Create task' })
    expect(create.hasAttribute('disabled')).toBe(true)

    releasePolicy()
    await waitFor(() => expect(create.hasAttribute('disabled')).toBe(false))
  })

  it('keeps create disabled after a policy load error and retries the exact board', async () => {
    const base = createPolicyRest()
    const attempts: string[] = []
    let failing = true

    const rest = vi.fn((path: string, opts?: PluginRestOptions) => {
      const url = new URL(path, 'http://kanban.test')
      const method = opts?.method ?? 'GET'

      if (method === 'GET' && (url.pathname === '/profiles' || url.pathname === '/orchestration')) {
        attempts.push(path)

        if (failing) {
          return Promise.reject(new Error('policy offline'))
        }
      }

      return base.rest(path, opts)
    }) as unknown as Rest

    bind(rest)
    $boardSlug.set('board-b')
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <NewTaskDialog onClose={vi.fn()} parents={[]} target="triage" />
      </QueryClientProvider>
    )

    fireEvent.change(await screen.findByPlaceholderText('Triage title'), { target: { value: 'Retry policy' } })
    expect(await screen.findByText('Could not load assignment policy')).toBeTruthy()
    const create = screen.getByRole('button', { name: 'Create task' })
    expect(create.hasAttribute('disabled')).toBe(true)

    failing = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(create.hasAttribute('disabled')).toBe(false))
    expect(attempts).toEqual([
      '/profiles?board=board-b',
      '/orchestration?board=board-b',
      '/profiles?board=board-b',
      '/orchestration?board=board-b'
    ])
  })

  it('sanitizes a stale explicit assignee against a deny/list transition in the actual create payload', async () => {
    const { boards, requests, rest } = createPolicyRest()
    bind(rest)
    $boardSlug.set('board-b')
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <NewTaskDialog onClose={vi.fn()} parents={[]} target="triage" />
      </QueryClientProvider>
    )

    fireEvent.change(await screen.findByPlaceholderText('Triage title'), { target: { value: 'Policy-safe task' } })
    const assignee = await screen.findByRole('combobox', { name: 'Assignee' })
    fireEvent.click(assignee)
    fireEvent.click(await screen.findByRole('option', { name: 'alpha' }))
    expect(assignee.textContent).toBe('alpha')

    boards['board-b'].allowed = ['beta']
    boards['board-b'].effective = ['beta']
    await act(async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: orchestrationKey('board-b') }),
        client.invalidateQueries({ queryKey: profilesKey('board-b') })
      ])
    })

    await waitFor(() => expect(assignee.textContent).toBe('Default (beta)'))
    const create = screen.getByRole('button', { name: 'Create task' })
    await waitFor(() => expect(create.hasAttribute('disabled')).toBe(false))
    fireEvent.click(create)

    await waitFor(() =>
      expect(
        requests.some(
          request =>
            request.method === 'POST' &&
            request.path === '/tasks?board=board-b' &&
            (request.body as { assignee?: string }).assignee === 'beta'
        )
      ).toBe(true)
    )
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['kanban', 'board', 'board-b'] }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: taskKey('board-b', 't-created') })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: BOARDS_KEY })
  })

  it('rejects an A estimate completion after A→B→A and keeps the newer A result', async () => {
    let releaseEstimate!: () => void

    const estimateGate = new Promise<void>(resolve => {
      releaseEstimate = resolve
    })

    const base = createPolicyRest()
    const estimateTitles: string[] = []

    const rest = vi.fn(async (path: string, opts?: PluginRestOptions) => {
      const url = new URL(path, 'http://kanban.test')

      if (url.pathname === '/estimate' && opts?.method === 'POST') {
        const title = String((opts.body as { title?: string }).title)
        estimateTitles.push(title)

        if (title === 'Old estimate') {
          await estimateGate

          return { complexity: 'S', est_tokens: 111, ok: true }
        }

        return { complexity: 'M', est_tokens: 222, ok: true }
      }

      return base.rest(path, opts)
    }) as unknown as Rest

    bind(rest)
    $boardSlug.set('board-a')
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <NewTaskDialog onClose={vi.fn()} parents={[]} target="triage" />
      </QueryClientProvider>
    )

    const title = await screen.findByPlaceholderText('Triage title')
    fireEvent.change(title, { target: { value: 'Old estimate' } })
    fireEvent.click(screen.getByRole('button', { name: 'Estimate' }))
    await waitFor(() => expect(estimateTitles).toEqual(['Old estimate']))

    act(() => $boardSlug.set('board-b'))
    await waitFor(() => expect((title as HTMLInputElement).value).toBe(''))
    act(() => $boardSlug.set('board-a'))
    await waitFor(() => expect((title as HTMLInputElement).value).toBe(''))

    fireEvent.change(title, { target: { value: 'Fresh estimate' } })
    fireEvent.click(screen.getByRole('button', { name: 'Estimate' }))
    expect(await screen.findByText(/~222/)).toBeTruthy()

    releaseEstimate()
    await waitFor(() => expect(estimateTitles).toEqual(['Old estimate', 'Fresh estimate']))
    expect(screen.getByText(/~222/)).toBeTruthy()
    expect(screen.queryByText(/~111/)).toBeNull()
  })

  it('rejects an A create completion after A→B→A and never closes or reports into the newer lifecycle', async () => {
    let releaseCreate!: () => void

    const createGate = new Promise<void>(resolve => {
      releaseCreate = resolve
    })

    const base = createPolicyRest()
    const createTitles: string[] = []

    const rest = vi.fn(async (path: string, opts?: PluginRestOptions) => {
      const url = new URL(path, 'http://kanban.test')

      if (url.pathname === '/tasks' && opts?.method === 'POST') {
        const title = String((opts.body as { title?: string }).title)
        createTitles.push(title)

        if (title === 'Old create') {
          await createGate

          return { task: { id: 'old-task', status: 'triage', title }, warning: 'stale warning' }
        }

        return { task: { id: 'fresh-task', status: 'triage', title } }
      }

      return base.rest(path, opts)
    }) as unknown as Rest

    bind(rest)
    $boardSlug.set('board-a')
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })
    const onClose = vi.fn()
    const notify = vi.spyOn(host, 'notify')
    clients.push(client)
    render(
      <QueryClientProvider client={client}>
        <ControlledNewTaskHarness onClose={onClose} />
      </QueryClientProvider>
    )

    const title = await screen.findByPlaceholderText('Triage title')
    fireEvent.change(title, { target: { value: 'Old create' } })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Create task' }).hasAttribute('disabled')).toBe(false)
    )
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }))
    await waitFor(() => expect(createTitles).toEqual(['Old create']))

    act(() => $boardSlug.set('board-b'))
    await waitFor(() => expect((title as HTMLInputElement).value).toBe(''))
    act(() => $boardSlug.set('board-a'))
    await waitFor(() => expect((title as HTMLInputElement).value).toBe(''))

    fireEvent.change(title, { target: { value: 'Fresh create' } })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Create task' }).hasAttribute('disabled')).toBe(false)
    )
    fireEvent.click(screen.getByRole('button', { name: 'Create task' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce())
    expect(onClose).toHaveBeenCalledWith('board-a')

    releaseCreate()
    await waitFor(() => expect(createTitles).toEqual(['Old create', 'Fresh create']))
    expect(onClose).toHaveBeenCalledOnce()
    expect(notify).not.toHaveBeenCalledWith(expect.objectContaining({ message: 'stale warning' }))
  })
})

describe('bulk selection lifecycle', () => {
  it('keeps a replacement selection and suppresses stale partial-failure UI', async () => {
    let releaseBulk!: () => void

    const bulkGate = new Promise<void>(resolve => {
      releaseBulk = resolve
    })

    const base = createPolicyRest()
    const bulkRequests: string[] = []

    const rest = vi.fn(async (path: string, opts?: PluginRestOptions) => {
      const url = new URL(path, 'http://kanban.test')

      if (url.pathname === '/tasks/bulk' && opts?.method === 'POST') {
        bulkRequests.push(path)
        await bulkGate

        return { results: [{ error: 'old refusal', id: 'old-task', ok: false }] }
      }

      return base.rest(path, opts)
    }) as unknown as Rest

    bind(rest)
    $boardSlug.set('board-a')

    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false, staleTime: Infinity } }
    })

    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const notify = vi.spyOn(host, 'notify')
    clients.push(client)
    client.setQueryData(profilesKey('board-a'), { profiles: [] })
    render(
      <QueryClientProvider client={client}>
        <BulkSelectionHarness />
      </QueryClientProvider>
    )

    fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(bulkRequests).toEqual(['/tasks/bulk?board=board-a']))
    fireEvent.click(screen.getByRole('button', { name: 'Replace selection' }))
    expect(screen.getByTestId('selection').textContent).toBe('new-task')

    releaseBulk()
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: taskKey('board-a', 'old-task') }))
    expect(screen.getByTestId('selection').textContent).toBe('new-task')
    expect(notify).not.toHaveBeenCalled()
  })
})

describe('per-board profile policy controls', () => {
  it('saves null for inheritance and starts an explicit policy from the effective roster', async () => {
    const { requests, rest } = createPolicyRest()
    renderPanel(rest, 'board-a')

    const inherit = await screen.findByRole('checkbox', { name: 'Inherit machine policy' })
    expect(inherit.getAttribute('aria-checked')).toBe('false')

    fireEvent.click(inherit)

    await waitFor(() =>
      expect(requests.some(request => request.method === 'PUT' && allowedProfiles(request.body) === null)).toBe(true)
    )

    act(() => $boardSlug.set('board-b'))
    const inherited = await screen.findByRole('checkbox', { name: 'Inherit machine policy' })
    await waitFor(() => expect(inherited.getAttribute('aria-checked')).toBe('true'))

    fireEvent.click(inherited)

    await waitFor(() =>
      expect(
        requests.some(
          request =>
            request.method === 'PUT' &&
            request.path === '/orchestration?board=board-b' &&
            JSON.stringify(allowedProfiles(request.body)) === JSON.stringify(['alpha', 'beta'])
        )
      ).toBe(true)
    )
  })

  it('warns clearly when the effective policy leaves no workers', async () => {
    const { rest } = createPolicyRest()
    renderPanel(rest, 'empty')

    expect((await screen.findByRole('alert')).textContent).toBe('No workers can run on this board.')
  })

  it('keeps a historically selected machine-blocked profile visible, checked, and disabled', async () => {
    const { rest } = createPolicyRest()
    renderPanel(rest)

    const blocked = await screen.findByRole('checkbox', { name: 'blocked — blocked by machine policy' })

    expect(blocked.getAttribute('aria-checked')).toBe('true')
    expect(blocked.hasAttribute('disabled')).toBe(true)
    expect(screen.getByText('blocked by machine policy')).toBeTruthy()
    expect(screen.getByDisplayValue('blocked description')).toBeTruthy()
  })

  it('saves only machine-permitted names and invalidates/refetches both board queries', async () => {
    const { requests, rest } = createPolicyRest()
    const client = renderPanel(rest)
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    const beta = await screen.findByRole('checkbox', { name: 'beta' })
    const initialGets = requests.filter(request => request.method === 'GET').length
    fireEvent.click(beta)

    await waitFor(() =>
      expect(
        requests.some(
          request =>
            request.method === 'PUT' &&
            request.path === '/orchestration?board=board-a' &&
            JSON.stringify(allowedProfiles(request.body)) === JSON.stringify(['alpha', 'beta'])
        )
      ).toBe(true)
    )
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: orchestrationKey('board-a') }))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: profilesKey('board-a') }))
    await waitFor(() =>
      expect(requests.filter(request => request.method === 'GET').length).toBeGreaterThan(initialGets)
    )
  })

  it('invalidates all orchestration caches for global fields while keeping roster invalidation board-scoped', async () => {
    const { requests, rest } = createPolicyRest()
    const client = renderPanel(rest)
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const autoDecompose = await screen.findByRole('switch', { name: 'Auto-decompose' })

    fireEvent.click(autoDecompose)

    await waitFor(() =>
      expect(
        requests.some(
          request =>
            request.method === 'PUT' && JSON.stringify(request.body) === JSON.stringify({ auto_decompose: false })
        )
      ).toBe(true)
    )
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ORCHESTRATION_KEY }))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: profilesKey('board-a') }))
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: ['kanban', 'profiles'] })
  })

  it('serializes orchestration saves and disables every panel control while the write is pending', async () => {
    let releasePut!: () => void

    const putGate = new Promise<void>(resolve => {
      releasePut = resolve
    })

    const { requests, rest } = createPolicyRest(putGate)
    renderPanel(rest)
    const autoDecompose = await screen.findByRole('switch', { name: 'Auto-decompose' })

    fireEvent.click(autoDecompose)
    fireEvent.click(autoDecompose)

    await waitFor(() => expect(requests.filter(request => request.method === 'PUT')).toHaveLength(1))
    expect(requests.find(request => request.method === 'PUT')?.body).toEqual({ auto_decompose: false })
    expect(screen.getAllByRole('combobox').every(control => control.hasAttribute('disabled'))).toBe(true)
    expect(screen.getAllByRole('checkbox').every(control => control.hasAttribute('disabled'))).toBe(true)
    expect(screen.getAllByRole('textbox').every(control => control.hasAttribute('disabled'))).toBe(true)
    expect(screen.getAllByRole('button').every(control => control.hasAttribute('disabled'))).toBe(true)

    releasePut()
    await waitFor(() => expect(autoDecompose.hasAttribute('disabled')).toBe(false))
    expect(requests.filter(request => request.method === 'PUT')).toHaveLength(1)
  })

  it('keeps the orchestration PUT lock across panel unmount and remount', async () => {
    let releasePut!: () => void

    const putGate = new Promise<void>(resolve => {
      releasePut = resolve
    })

    const { requests, rest } = createPolicyRest(putGate)
    bind(rest)
    $boardSlug.set('board-a')

    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false, staleTime: Infinity } }
    })

    clients.push(client)

    const renderCurrent = () =>
      render(
        <QueryClientProvider client={client}>
          <OrchestrationPanel />
        </QueryClientProvider>
      )

    const first = renderCurrent()
    fireEvent.click(await screen.findByRole('switch', { name: 'Auto-decompose' }))
    await waitFor(() => expect(requests.filter(request => request.method === 'PUT')).toHaveLength(1))

    first.unmount()
    renderCurrent()
    const remounted = await screen.findByRole('switch', { name: 'Auto-decompose' })
    expect(remounted.hasAttribute('disabled')).toBe(true)
    fireEvent.click(remounted)
    expect(requests.filter(request => request.method === 'PUT')).toHaveLength(1)

    releasePut()
    await waitFor(() => expect(remounted.hasAttribute('disabled')).toBe(false))
    expect(requests.filter(request => request.method === 'PUT')).toHaveLength(1)
  })

  it('keeps profile Save and Auto globally locked across a board-switch remount', async () => {
    let releaseWrite!: () => void

    const writeGate = new Promise<void>(resolve => {
      releaseWrite = resolve
    })

    const base = createPolicyRest()
    const writes: Array<{ method: string; path: string }> = []

    const rest = vi.fn((path: string, opts?: PluginRestOptions) => {
      const url = new URL(path, 'http://kanban.test')
      const method = opts?.method ?? 'GET'

      if (/^\/profiles\/[^/]+(?:\/describe-auto)?$/.test(url.pathname) && method !== 'GET') {
        writes.push({ method, path })

        return writeGate.then(() => (method === 'POST' ? { description: 'Generated', ok: true } : {}))
      }

      return base.rest(path, opts)
    }) as unknown as Rest

    bind(rest)
    $boardSlug.set('board-a')

    const client = new QueryClient({
      defaultOptions: { mutations: { retry: false }, queries: { retry: false, staleTime: Infinity } }
    })

    clients.push(client)

    const renderCurrent = () =>
      render(
        <QueryClientProvider client={client}>
          <OrchestrationPanel />
        </QueryClientProvider>
      )

    const first = renderCurrent()
    const description = await screen.findByDisplayValue('alpha description')
    fireEvent.change(description, { target: { value: 'Updated alpha' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[0])
    await waitFor(() => expect(writes).toHaveLength(1))

    first.unmount()
    act(() => $boardSlug.set('board-b'))
    renderCurrent()
    await screen.findByDisplayValue('alpha description')
    const auto = screen.getAllByRole('button', { name: 'Auto' })[0]
    expect(auto.hasAttribute('disabled')).toBe(true)
    fireEvent.click(auto)
    expect(writes).toHaveLength(1)

    releaseWrite()
    await waitFor(() => expect(screen.getAllByRole('button', { name: 'Auto' })[0].hasAttribute('disabled')).toBe(false))
    expect(writes).toEqual([{ method: 'PATCH', path: '/profiles/alpha' }])
  })

  it('invalidates every board profile cache after a global description save', async () => {
    const { requests, rest } = createPolicyRest()
    const client = renderPanel(rest)
    client.setQueryData(profilesKey('board-b'), { profiles: [] })
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    const description = await screen.findByDisplayValue('alpha description')
    fireEvent.change(description, { target: { value: 'Updated alpha' } })
    fireEvent.click(screen.getAllByRole('button', { name: 'Save' })[0])

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: PROFILES_KEY }))
    expect(client.getQueryState(profilesKey('board-b'))?.isInvalidated).toBe(true)
    expect(requests).toContainEqual({
      body: { description: 'Updated alpha' },
      method: 'PATCH',
      path: '/profiles/alpha'
    })
  })

  it('renders a terminal load error with a working retry instead of an empty panel', async () => {
    const base = createPolicyRest()
    let failing = true

    const rest = vi.fn((path: string, opts?: PluginRestOptions) => {
      if (failing && (opts?.method ?? 'GET') === 'GET') {
        return Promise.reject(new Error('offline'))
      }

      return base.rest(path, opts)
    }) as unknown as Rest

    renderPanel(rest)

    expect(await screen.findByText('Could not load orchestration settings')).toBeTruthy()
    failing = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByRole('checkbox', { name: 'Inherit machine policy' })).toBeTruthy()
  })

  it('shows blocked configured selector truth and preserves it while saving an unrelated field', async () => {
    const { requests, rest } = createPolicyRest()
    renderPanel(rest)

    const [orchestrator, defaultAssignee] = await screen.findAllByRole('combobox')
    expect(orchestrator.textContent).toBe('alpha')
    expect(defaultAssignee.textContent).toBe('beta (configured, blocked)')
    expect(screen.getByRole('status').textContent).toBe('Effective fallback: alpha')
    fireEvent.click(defaultAssignee)
    const blockedConfigured = await screen.findByRole('option', { name: 'beta (configured, blocked)' })
    expect(blockedConfigured.getAttribute('aria-disabled')).toBe('true')
    expect(screen.queryByRole('option', { name: 'beta' })).toBeNull()
    fireEvent.keyDown(window.document, { key: 'Escape' })
    fireEvent.click(orchestrator)

    expect(await screen.findByRole('option', { name: 'alpha' })).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'beta' })).toBeNull()
    expect(screen.queryByRole('option', { name: 'blocked' })).toBeNull()
    fireEvent.keyDown(window.document, { key: 'Escape' })
    expect(screen.getByDisplayValue('alpha description')).toBeTruthy()
    expect(screen.getByDisplayValue('beta description')).toBeTruthy()
    expect(screen.getByDisplayValue('blocked description')).toBeTruthy()

    fireEvent.click(screen.getByRole('switch', { name: 'Auto-decompose' }))
    await waitFor(() =>
      expect(requests).toContainEqual({
        body: { auto_decompose: false },
        method: 'PUT',
        path: '/orchestration?board=board-a'
      })
    )
    expect(defaultAssignee.textContent).toBe('beta (configured, blocked)')
  })
})
