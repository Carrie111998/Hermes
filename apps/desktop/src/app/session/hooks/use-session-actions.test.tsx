import { act, cleanup, render, waitFor } from '@testing-library/react'
import type { MutableRefObject } from 'react'
import { useEffect } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { noteActiveTreeGroup, revealTreePane } from '@/components/pane-shell/tree/store'
import { getSession, getSessionMessages, type SessionInfo } from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import { clearSessionDraft, stashSessionDraft, takeSessionDraft } from '@/store/composer'
import { $activeGatewayProfile, $newChatProfile, ensureGatewayProfile } from '@/store/profile'
import { $projectScope, $projectTree, ALL_PROJECTS } from '@/store/projects'
import {
  $activeSessionId,
  $activeSessionStoredIdRotation,
  $currentBranch,
  $currentCwd,
  $currentFastMode,
  $currentModel,
  $currentProvider,
  $currentReasoningEffort,
  $messages,
  $newChatWorkspaceTarget,
  $resumeFailedSessionId,
  $selectedStoredSessionId,
  $sessions,
  $workspaceCwdOwner,
  getRememberedWorkspaceCwd,
  releaseWorkspaceCwdOwner,
  setActiveSessionId,
  setActiveSessionStoredIdRotation,
  setCurrentBranch,
  setCurrentCwd,
  setCurrentFastMode,
  setCurrentModel,
  setCurrentProvider,
  setCurrentReasoningEffort,
  setMessages,
  setNewChatWorkspaceTarget,
  setResumeFailedSessionId,
  setSelectedStoredSessionId,
  setSessions,
  setWorkspaceCwdOwner,
  workspaceCwdBelongsToSelectedSession
} from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

import { sessionRoute } from '../../routes'
import type { ClientSessionState } from '../../types'

import { useSessionActions } from './use-session-actions'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: vi.fn(),
  getSession: vi.fn(),
  getSessionMessages: vi.fn(),
  listAllProfileSessions: vi.fn(),
  setApiRequestProfile: vi.fn(),
  setSessionArchived: vi.fn()
}))

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn().mockResolvedValue(undefined)
}))

vi.mock('@/components/pane-shell/tree/store', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  noteActiveTreeGroup: vi.fn(),
  revealTreePane: vi.fn()
}))

const RUNTIME_SESSION_ID = 'rt-new-001'

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

type HarnessHandle = Pick<
  ReturnType<typeof useSessionActions>,
  'createBackendSessionForSend' | 'openNewSessionTile' | 'selectSidebarItem' | 'startFreshSessionDraft'
>

function storedSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    ended_at: null,
    id: 'stored-1',
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 0,
    model: null,
    output_tokens: 0,
    preview: null,
    source: 'desktop',
    started_at: 1,
    title: 'stored',
    tool_call_count: 0,
    ...overrides
  }
}

function Harness({
  navigate = vi.fn(),
  onReady,
  requestGateway
}: {
  navigate?: ReturnType<typeof vi.fn>
  onReady: (handle: HarnessHandle) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    navigate: navigate as never,
    requestGateway,
    resetViewSync: vi.fn(),
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady(actions)
  }, [actions, onReady])

  return null
}

function StoredIdRotationHarness({
  activeSessionIdRef,
  getRoutedStoredSessionId,
  navigate,
  selectedStoredSessionIdRef
}: {
  activeSessionIdRef: MutableRefObject<string | null>
  getRoutedStoredSessionId: () => null | string
  navigate: (to: string, options?: { replace?: boolean }) => void
  selectedStoredSessionIdRef: MutableRefObject<string | null>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  useSessionActions({
    activeSessionId: activeSessionIdRef.current,
    activeSessionIdRef,
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId,
    navigate: navigate as never,
    requestGateway: async () => ({}) as never,
    resetViewSync: vi.fn(),
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: selectedStoredSessionIdRef.current,
    selectedStoredSessionIdRef,
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  return null
}

describe('active stored-session id rotation routing', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setActiveSessionStoredIdRotation(null)
    setSelectedStoredSessionId(null)
    vi.restoreAllMocks()
  })

  it('follows a rotation while the same conversation still owns the foreground route', async () => {
    const activeSessionIdRef: MutableRefObject<string | null> = { current: 'runtime-A' }
    const selectedStoredSessionIdRef: MutableRefObject<string | null> = { current: 'stored-A' }
    const navigate = vi.fn()

    setSelectedStoredSessionId('stored-A')
    render(
      <StoredIdRotationHarness
        activeSessionIdRef={activeSessionIdRef}
        getRoutedStoredSessionId={() => 'stored-A'}
        navigate={navigate}
        selectedStoredSessionIdRef={selectedStoredSessionIdRef}
      />
    )

    act(() => {
      setActiveSessionStoredIdRotation({
        nextStoredSessionId: 'stored-A-next',
        previousStoredSessionId: 'stored-A',
        runtimeSessionId: 'runtime-A'
      })
    })

    await waitFor(() => expect(selectedStoredSessionIdRef.current).toBe('stored-A-next'))
    expect($selectedStoredSessionId.get()).toBe('stored-A-next')
    expect(navigate).toHaveBeenCalledWith(sessionRoute('stored-A-next'), { replace: true })
    expect($activeSessionStoredIdRotation.get()).toBeNull()
  })

  it('keeps draft on the previous tip when the new tip row is not loaded yet', async () => {
    const tipBefore = 'tip-root'
    const tipAfter = 'tip-new-unloaded'
    const runtimeSessionId = 'runtime-gap'
    const activeSessionIdRef: MutableRefObject<string | null> = { current: runtimeSessionId }
    const selectedStoredSessionIdRef: MutableRefObject<string | null> = { current: tipBefore }
    const navigate = vi.fn()

    setSessions([])
    stashSessionDraft(tipBefore, 'typed during gap', [])
    setSelectedStoredSessionId(tipBefore)
    setActiveSessionId(runtimeSessionId)

    render(
      <StoredIdRotationHarness
        activeSessionIdRef={activeSessionIdRef}
        getRoutedStoredSessionId={() => tipBefore}
        navigate={navigate}
        selectedStoredSessionIdRef={selectedStoredSessionIdRef}
      />
    )

    act(() => {
      setActiveSessionStoredIdRotation({
        nextStoredSessionId: tipAfter,
        previousStoredSessionId: tipBefore,
        runtimeSessionId
      })
    })

    await waitFor(() => expect($selectedStoredSessionId.get()).toBe(tipAfter))
    expect(takeSessionDraft(tipBefore).text).toBe('typed during gap')
    expect(takeSessionDraft(tipAfter).text).toBe('')

    clearSessionDraft(tipBefore)
    clearSessionDraft(tipAfter)
    setActiveSessionId(null)
  })

  it('parks an in-progress composer draft on the lineage root across tip rotation', async () => {
    // Desktop draft must stay on the durable composer key (lineage root), not
    // move onto the fresh tip — ChatBar scopes drafts via resolveComposerSessionKey.
    const tipBefore = '20260720_062637_ad96b3'
    const tipAfter = '20260720_071049_a28905'
    const runtimeSessionId = 'runtime-desktop-thinking'
    const activeSessionIdRef: MutableRefObject<string | null> = { current: runtimeSessionId }
    const selectedStoredSessionIdRef: MutableRefObject<string | null> = { current: tipBefore }
    const navigate = vi.fn()
    const typedWhileThinking = 'follow up I am still typing during thinking'

    setSessions([storedSession({ id: tipAfter, message_count: 2, _lineage_root_id: tipBefore })])
    stashSessionDraft(tipBefore, typedWhileThinking, [])
    setSelectedStoredSessionId(tipBefore)
    setActiveSessionId(runtimeSessionId)

    render(
      <StoredIdRotationHarness
        activeSessionIdRef={activeSessionIdRef}
        getRoutedStoredSessionId={() => tipBefore}
        navigate={navigate}
        selectedStoredSessionIdRef={selectedStoredSessionIdRef}
      />
    )

    act(() => {
      setActiveSessionStoredIdRotation({
        nextStoredSessionId: tipAfter,
        previousStoredSessionId: tipBefore,
        runtimeSessionId
      })
    })

    await waitFor(() => expect($selectedStoredSessionId.get()).toBe(tipAfter))
    // Durable key remains the lineage root — same scope ChatBar will keep using.
    expect(takeSessionDraft(tipBefore).text).toBe(typedWhileThinking)
    expect(takeSessionDraft(tipAfter).text).toBe('')

    clearSessionDraft(tipBefore)
    clearSessionDraft(tipAfter)
    setActiveSessionId(null)
    setSessions([])
  })

  it('does not overwrite a newer route intent before its resume effect has synchronized selection', async () => {
    const activeSessionIdRef: MutableRefObject<string | null> = { current: 'runtime-A' }
    const selectedStoredSessionIdRef: MutableRefObject<string | null> = { current: 'stored-A' }
    const navigate = vi.fn()

    setSelectedStoredSessionId('stored-A')
    render(
      <StoredIdRotationHarness
        activeSessionIdRef={activeSessionIdRef}
        getRoutedStoredSessionId={() => 'stored-C'}
        navigate={navigate}
        selectedStoredSessionIdRef={selectedStoredSessionIdRef}
      />
    )

    act(() => {
      setActiveSessionStoredIdRotation({
        nextStoredSessionId: 'stored-A-next',
        previousStoredSessionId: 'stored-A',
        runtimeSessionId: 'runtime-A'
      })
    })

    await waitFor(() => expect($activeSessionStoredIdRotation.get()).toBeNull())
    expect(selectedStoredSessionIdRef.current).toBe('stored-A')
    expect($selectedStoredSessionId.get()).toBe('stored-A')
    expect(navigate).not.toHaveBeenCalled()
  })

  it('does not let the previous runtime jump back after selection already moved', async () => {
    const activeSessionIdRef: MutableRefObject<string | null> = { current: 'runtime-A' }
    const selectedStoredSessionIdRef: MutableRefObject<string | null> = { current: 'stored-C' }
    const navigate = vi.fn()

    setSelectedStoredSessionId('stored-C')
    render(
      <StoredIdRotationHarness
        activeSessionIdRef={activeSessionIdRef}
        getRoutedStoredSessionId={() => 'stored-C'}
        navigate={navigate}
        selectedStoredSessionIdRef={selectedStoredSessionIdRef}
      />
    )

    act(() => {
      setActiveSessionStoredIdRotation({
        nextStoredSessionId: 'stored-A-next',
        previousStoredSessionId: 'stored-A',
        runtimeSessionId: 'runtime-A'
      })
    })

    await waitFor(() => expect($activeSessionStoredIdRotation.get()).toBeNull())
    expect(selectedStoredSessionIdRef.current).toBe('stored-C')
    expect($selectedStoredSessionId.get()).toBe('stored-C')
    expect(navigate).not.toHaveBeenCalled()
  })

  it('updates the underlying selection without navigating out of an overlay or page', async () => {
    const activeSessionIdRef: MutableRefObject<string | null> = { current: 'runtime-A' }
    const selectedStoredSessionIdRef: MutableRefObject<string | null> = { current: 'stored-A' }
    const navigate = vi.fn()

    setSelectedStoredSessionId('stored-A')
    render(
      <StoredIdRotationHarness
        activeSessionIdRef={activeSessionIdRef}
        getRoutedStoredSessionId={() => null}
        navigate={navigate}
        selectedStoredSessionIdRef={selectedStoredSessionIdRef}
      />
    )

    act(() => {
      setActiveSessionStoredIdRotation({
        nextStoredSessionId: 'stored-A-next',
        previousStoredSessionId: 'stored-A',
        runtimeSessionId: 'runtime-A'
      })
    })

    await waitFor(() => expect(selectedStoredSessionIdRef.current).toBe('stored-A-next'))
    expect($selectedStoredSessionId.get()).toBe('stored-A-next')
    expect(navigate).not.toHaveBeenCalled()
  })
})

async function createWith(
  profileSetup: () => void,
  beforeCreate?: (handle: HarnessHandle) => Promise<void> | void
): Promise<Record<string, unknown> | undefined> {
  let createParams: Record<string, unknown> | undefined

  const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
    if (method === 'session.create') {
      createParams = params

      return { session_id: RUNTIME_SESSION_ID, stored_session_id: null } as never
    }

    return {} as never
  })

  setCurrentCwd('')
  setNewChatWorkspaceTarget(undefined)
  profileSetup()

  let handle: HarnessHandle | null = null
  render(<Harness onReady={h => (handle = h)} requestGateway={requestGateway} />)
  await waitFor(() => expect(handle).not.toBeNull())

  if (beforeCreate) {
    await act(async () => {
      await beforeCreate(handle!)
    })
  }

  await act(async () => {
    await handle!.createBackendSessionForSend()
  })

  return createParams
}

describe('startFreshSessionDraft', () => {
  afterEach(() => cleanup())

  it('can reset machine-bound session state without closing the current overlay route', async () => {
    const navigate = vi.fn()
    const requestGateway = vi.fn(async () => ({}) as never)
    let handle: HarnessHandle | null = null

    render(<Harness navigate={navigate} onReady={value => (handle = value)} requestGateway={requestGateway} />)
    await waitFor(() => expect(handle).not.toBeNull())

    act(() => handle!.startFreshSessionDraft({ preserveRoute: true, workspaceTarget: null }))

    expect(navigate).not.toHaveBeenCalled()
    expect($currentCwd.get()).toBe('')
    expect($newChatWorkspaceTarget.get()).toBeNull()
  })
})

describe('createBackendSessionForSend profile routing', () => {
  afterEach(() => {
    cleanup()
    $newChatProfile.set(null)
    $activeGatewayProfile.set('default')
    $projectScope.set(ALL_PROJECTS)
    $projectTree.set([])
    $currentCwd.set('')
    $currentFastMode.set(false)
    $currentModel.set('')
    $currentProvider.set('')
    $currentReasoningEffort.set('')
    setNewChatWorkspaceTarget(undefined)
    vi.restoreAllMocks()
  })

  it('routes a plain new chat (no explicit profile) to the live gateway profile', async () => {
    // The "rubberband to default" bug: the top New Session button clears
    // $newChatProfile to null. In global-remote mode one backend serves every
    // profile, so an omitted `profile` lands the chat on the launch (default)
    // profile. The session must instead carry the active gateway profile.
    const params = await createWith(() => {
      $activeGatewayProfile.set('coder')
      $newChatProfile.set(null)
    })

    expect(params).toMatchObject({ profile: 'coder' })
  })

  it('honours an explicit per-profile "+" selection', async () => {
    const params = await createWith(() => {
      $activeGatewayProfile.set('coder')
      $newChatProfile.set('analyst')
    })

    expect(params).toMatchObject({ profile: 'analyst' })
  })

  it('passes the default profile for single-profile users (backend resolves it to launch)', async () => {
    const params = await createWith(() => {
      $activeGatewayProfile.set('default')
      $newChatProfile.set(null)
    })

    expect(params).toMatchObject({ profile: 'default' })
  })

  it('tags new desktop chats as desktop sessions', async () => {
    const params = await createWith(() => {})

    expect(params).toMatchObject({ source: 'desktop' })
  })

  it('passes the current workspace cwd into session.create', async () => {
    const params = await createWith(() => {
      $currentCwd.set('/remote/worktree')
    })

    expect(params).toMatchObject({ cwd: '/remote/worktree' })
  })

  it('freezes the visible selector state before profile readiness and sends fast: false explicitly', async () => {
    const profileReady = deferred<void>()
    vi.mocked(ensureGatewayProfile).mockReturnValueOnce(profileReady.promise)

    setCurrentModel('anthropic/claude-sonnet-4.6')
    setCurrentProvider('anthropic')
    setCurrentReasoningEffort('high')
    setCurrentFastMode(false)

    let createParams: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.create') {
        createParams = params

        return { session_id: RUNTIME_SESSION_ID, stored_session_id: null } as never
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    render(<Harness onReady={next => (handle = next)} requestGateway={requestGateway} />)
    await waitFor(() => expect(handle).not.toBeNull())

    let createPromise!: Promise<null | string>
    act(() => {
      createPromise = handle!.createBackendSessionForSend()
    })
    await waitFor(() => expect(ensureGatewayProfile).toHaveBeenCalled())

    // A background refresh or a second click can mutate the sticky atoms while
    // the profile is waking. This send must still use what was visible at Enter.
    setCurrentModel('openai/gpt-5.5')
    setCurrentProvider('openai-codex')
    setCurrentReasoningEffort('low')
    setCurrentFastMode(true)
    profileReady.resolve()

    await act(async () => {
      await createPromise
    })

    expect(createParams).toMatchObject({
      fast: false,
      model: 'anthropic/claude-sonnet-4.6',
      provider: 'anthropic',
      reasoning_effort: 'high'
    })
  })

  it('falls back to the entered project cwd when the current cwd is blank', async () => {
    const params = await createWith(() => {
      $projectTree.set([
        {
          id: 'p_app',
          label: 'App',
          path: '/repo/app',
          repos: [{ groups: [], id: '/repo/app', label: 'app', path: '/repo/app', sessionCount: 0 }],
          sessionCount: 0
        }
      ])
      $projectScope.set('p_app')
      $currentCwd.set('')
    })

    expect(params).toMatchObject({ cwd: '/repo/app' })
  })
})

// ── Resume failure recovery (the "stuck loading session window" bug) ──────────
// When session.resume rejects AND the REST transcript fallback ALSO fails, the
// hook must (a) not throw out of the fallback (which stranded the loader), and
// (b) arm $resumeFailedSessionId so use-route-resume can retry. A resume that
// succeeds must NOT leave the flag armed.
function ResumeHarness({
  onStateUpdate,
  onReady,
  requestGateway,
  runtimeIdByStoredSessionIdRef,
  selectedStoredSessionId = null,
  sessionStateByRuntimeIdRef
}: {
  onStateUpdate?: (sessionId: string, state: ClientSessionState) => void
  onReady: (resume: (storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  runtimeIdByStoredSessionIdRef?: MutableRefObject<Map<string, string>>
  selectedStoredSessionId?: string | null
  sessionStateByRuntimeIdRef?: MutableRefObject<Map<string, ClientSessionState>>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId: null,
    activeSessionIdRef: ref<string | null>(null),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    navigate: vi.fn() as never,
    requestGateway,
    resetViewSync: vi.fn(),
    runtimeIdByStoredSessionIdRef: runtimeIdByStoredSessionIdRef ?? ref(new Map<string, string>()),
    selectedStoredSessionId,
    selectedStoredSessionIdRef: ref<string | null>(selectedStoredSessionId),
    sessionStateByRuntimeIdRef: sessionStateByRuntimeIdRef ?? ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: (sessionId, updater) => {
      const next = updater({} as ClientSessionState)
      onStateUpdate?.(sessionId, next)

      return next
    }
  })

  useEffect(() => {
    onReady(actions.resumeSession)
  }, [actions.resumeSession, onReady])

  return null
}

describe('resumeSession failure recovery', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setResumeFailedSessionId(null)
    setMessages([])
    setSessions([])
    vi.restoreAllMocks()
  })

  async function runResume(
    requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>,
    options: {
      runtimeIdByStoredSessionIdRef?: MutableRefObject<Map<string, string>>
      sessionStateByRuntimeIdRef?: MutableRefObject<Map<string, ClientSessionState>>
    } = {}
  ): Promise<void> {
    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null
    render(<ResumeHarness onReady={r => (resume = r)} requestGateway={requestGateway} {...options} />)
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-1', true)
  }

  it('arms $resumeFailedSessionId when resume RPC and REST fallback both fail', async () => {
    // session.resume rejects (e.g. timeout against a wedged backend)...
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        throw new Error('request timed out: session.resume')
      }

      return {} as never
    })

    // ...and the REST transcript fallback also rejects (backend unreachable).
    vi.mocked(getSessionMessages).mockRejectedValue(new Error('network down'))

    await runResume(requestGateway)

    // The window is no longer silently stranded: the failure latch is armed for
    // the stored session, which use-route-resume consumes to retry.
    expect($resumeFailedSessionId.get()).toBe('stored-1')
  })

  it('does NOT arm the failure latch when the resume RPC fails but the REST fallback paints history', async () => {
    // session.resume rejects, but the REST transcript fallback succeeds and
    // hydrates a readable transcript — the window is NOT stranded.
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        throw new Error('request timed out: session.resume')
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: [
        { content: 'hello', role: 'user', timestamp: 1 },
        { content: 'hi there', role: 'assistant', timestamp: 2 }
      ],
      session_id: 'stored-1'
    } as never)

    await runResume(requestGateway)

    // Arming here would auto-retry a window that already shows history and,
    // on exhaustion, blank that transcript behind the error overlay — a
    // regression vs. plain fallback-success. The latch must stay clear.
    expect($resumeFailedSessionId.get()).toBeNull()
    // The fallback transcript is visible.
    expect($messages.get().length).toBeGreaterThan(0)
  })

  it('preserves an optimistic user message during a same-session reconnect', async () => {
    setMessages([
      {
        id: 'stored-user',
        role: 'user',
        parts: [{ type: 'text', text: 'earlier question' }]
      },
      {
        id: 'stored-assistant',
        role: 'assistant',
        parts: [{ type: 'text', text: 'earlier answer' }]
      },
      {
        id: 'user-optimistic',
        role: 'user',
        parts: [{ type: 'text', text: 'message sent during reconnect' }]
      }
    ])

    const storedMessages = [
      { content: 'earlier question', role: 'user', timestamp: 1 },
      { content: 'earlier answer', role: 'assistant', timestamp: 2 }
    ]

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: storedMessages, session_id: 'stored-1' } as never)

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        return {
          session_id: 'runtime-1',
          session_key: 'stored-1',
          resumed: 'stored-1',
          message_count: 2,
          messages: storedMessages,
          info: {}
        } as never
      }

      return {} as never
    })

    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null
    render(
      <ResumeHarness onReady={r => (resume = r)} requestGateway={requestGateway} selectedStoredSessionId="stored-1" />
    )
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-1', true)

    expect($messages.get().map(message => message.id)).toContain('user-optimistic')
  })

  it('restores the in-flight turn and queued user prompt after a full renderer restart', async () => {
    const storedMessages = [
      { content: 'earlier question', role: 'user', timestamp: 1 },
      { content: 'earlier answer', role: 'assistant', timestamp: 2 }
    ]

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: storedMessages, session_id: 'stored-1' } as never)

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        return {
          session_id: 'runtime-1',
          session_key: 'stored-1',
          resumed: 'stored-1',
          message_count: storedMessages.length,
          messages: storedMessages,
          running: true,
          inflight: {
            user: 'current prompt',
            assistant: 'partial answer',
            streaming: true
          },
          queued: { user: 'newest prompt' },
          info: {}
        } as never
      }

      return {} as never
    })

    let resumedState: ClientSessionState | undefined
    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null
    render(
      <ResumeHarness
        onReady={ready => (resume = ready)}
        onStateUpdate={(_sessionId, state) => (resumedState = state)}
        requestGateway={requestGateway}
      />
    )
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-1', true)

    const renderedMessages = JSON.stringify(resumedState?.messages)
    expect(renderedMessages).toContain('current prompt')
    expect(renderedMessages).toContain('partial answer')
    expect(renderedMessages).toContain('newest prompt')
  })

  it('uses the continuation projection when resume rotates an equal-length stored transcript', async () => {
    const parentMessages = [
      { content: 'question before compression', role: 'user', timestamp: 1 },
      { content: 'answer before compression', role: 'assistant', timestamp: 2 }
    ]

    const continuationMessages = [
      { content: 'prompt after compression', role: 'user', timestamp: 3 },
      { content: 'answer after compression', role: 'assistant', timestamp: 4 }
    ]

    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: parentMessages,
      session_id: 'stored-1'
    } as never)

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        return {
          session_id: 'runtime-continuation',
          session_key: 'stored-continuation',
          resumed: 'stored-continuation',
          message_count: continuationMessages.length,
          messages: continuationMessages,
          info: {}
        } as never
      }

      return {} as never
    })

    let resumedState: ClientSessionState | undefined
    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null

    render(
      <ResumeHarness
        onReady={ready => (resume = ready)}
        onStateUpdate={(_sessionId, state) => (resumedState = state)}
        requestGateway={requestGateway}
      />
    )
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-1', true)

    const renderedMessages = JSON.stringify(resumedState?.messages)
    expect(renderedMessages).toContain('prompt after compression')
    expect(renderedMessages).toContain('answer after compression')
    expect(renderedMessages).not.toContain('answer before compression')
  })

  it('does NOT throw out of the fallback when REST also fails (no unhandled rejection)', async () => {
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        throw new Error('request timed out: session.resume')
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockRejectedValue(new Error('network down'))

    // resumeSession must resolve (swallow the fallback failure), not reject.
    await expect(runResume(requestGateway)).resolves.toBeUndefined()
  })

  it('leaves the failure latch clear when resume succeeds', async () => {
    // Pre-arm to prove a successful resume clears it (entry-clear path).
    setResumeFailedSessionId('stored-1')

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        return { session_id: 'runtime-1', resumed: params?.session_id, messages: [], info: {} } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [] } as never)

    await runResume(requestGateway)

    expect($resumeFailedSessionId.get()).toBeNull()
  })

  it('resumes via the gateway default (deferred build) — not lazy, no eager opt-out', async () => {
    // The switch-latency fix lives backend-side: a normal cold resume gets the
    // gateway's default DEFERRED build (transcript returns immediately, agent
    // pre-warms in the background). The client must NOT force the synchronous
    // path (eager_build) and is only `lazy` for subagent watch windows.
    let resumeParams: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        resumeParams = params

        return { session_id: 'runtime-1', resumed: params?.session_id, messages: [], info: {} } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [] } as never)

    await runResume(requestGateway)

    expect(resumeParams).not.toHaveProperty('lazy')
    expect(resumeParams).not.toHaveProperty('eager_build')
    expect(resumeParams).toMatchObject({ source: 'desktop', omit_messages: true })
  })

  it('arms the failure latch when resume succeeds with an empty transcript for a non-empty stored session', async () => {
    setSessions([storedSession({ message_count: 4 })])

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        return { session_id: 'runtime-1', resumed: params?.session_id, messages: [], info: {} } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [], session_id: 'stored-1' } as never)

    await runResume(requestGateway)

    expect($resumeFailedSessionId.get()).toBe('stored-1')
    expect($activeSessionId.get()).toBeNull()
    expect($messages.get()).toEqual([])
  })

  it('does not reuse an empty cached runtime view for a stored session with history', async () => {
    const runtimeIdByStoredSessionIdRef = {
      current: new Map([['stored-1', 'runtime-stale']])
    } satisfies MutableRefObject<Map<string, string>>

    const sessionStateByRuntimeIdRef = {
      current: new Map([
        [
          'runtime-stale',
          {
            awaitingResponse: false,
            branch: '',
            busy: false,
            cwd: '',
            fast: false,
            interimBoundaryPending: false,
            interrupted: false,
            messages: [],
            model: '',
            needsInput: false,
            pendingBranchGroup: null,
            personality: '',
            provider: '',
            reasoningEffort: '',
            sawAssistantPayload: false,
            serviceTier: '',
            storedSessionId: 'stored-1',
            streamId: null,
            turnStartedAt: null,
            usage: null,
            yolo: false
          }
        ]
      ])
    } satisfies MutableRefObject<Map<string, ClientSessionState>>

    setSessions([storedSession({ message_count: 4 })])

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        return { session_id: 'runtime-1', resumed: params?.session_id, messages: [], info: {} } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: [{ content: 'existing text', role: 'user', timestamp: 1 }],
      session_id: 'stored-1'
    } as never)

    await runResume(requestGateway, {
      runtimeIdByStoredSessionIdRef,
      sessionStateByRuntimeIdRef
    })

    expect(requestGateway).not.toHaveBeenCalledWith('session.usage', { session_id: 'runtime-stale' })
    expect(runtimeIdByStoredSessionIdRef.current.has('stored-1')).toBe(false)
    expect(sessionStateByRuntimeIdRef.current.has('runtime-stale')).toBe(false)
    expect($activeSessionId.get()).toBe('runtime-1')
    expect($messages.get().length).toBe(1)
  })
})

function BranchHarness({
  activeSessionId = null,
  navigate = vi.fn(),
  onCurrentReady,
  onReady,
  requestGateway
}: {
  activeSessionId?: string | null
  navigate?: ReturnType<typeof vi.fn>
  onCurrentReady?: (branchCurrentSession: (messageId?: string) => Promise<boolean>) => void
  onReady: (branchStoredSession: (storedSessionId: string, sessionProfile?: string | null) => Promise<boolean>) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId,
    activeSessionIdRef: ref<string | null>(activeSessionId),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => null,
    navigate: navigate as never,
    requestGateway,
    resetViewSync: vi.fn(),
    runtimeIdByStoredSessionIdRef: ref(new Map<string, string>()),
    selectedStoredSessionId: null,
    selectedStoredSessionIdRef: ref<string | null>(null),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady(actions.branchStoredSession)
    onCurrentReady?.(actions.branchCurrentSession)
  }, [actions.branchCurrentSession, actions.branchStoredSession, onCurrentReady, onReady])

  return null
}

describe('branchStoredSession desktop source tagging', () => {
  afterEach(() => {
    cleanup()
    setSessions([])
    $sessionTiles.set([])
    setSelectedStoredSessionId(null)
    vi.restoreAllMocks()
  })

  it('opens the branch as a new tab and leaves the parent chat selected', async () => {
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.create') {
        return { session_id: 'branch-runtime', stored_session_id: 'branch-stored' } as never
      }

      return {} as never
    })

    // Parent is the currently-open (primary) chat.
    setSessions([storedSession({ id: 'stored-parent', message_count: 1 })])
    setSelectedStoredSessionId('stored-parent')
    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: [{ content: 'branch me', role: 'user', timestamp: 1 }],
      session_id: 'stored-parent'
    } as never)

    const navigate = vi.fn()
    let branchStoredSession: ((storedSessionId: string) => Promise<boolean>) | null = null
    render(
      <BranchHarness
        navigate={navigate}
        onReady={branch => (branchStoredSession = branch)}
        requestGateway={requestGateway}
      />
    )
    await waitFor(() => expect(branchStoredSession).not.toBeNull())

    await expect(branchStoredSession!('stored-parent')).resolves.toBe(true)

    // The branch opened as its own tab...
    expect($sessionTiles.get().some(tile => tile.storedSessionId === 'branch-stored')).toBe(true)
    // ...without stealing the primary selection or navigating away from the parent.
    expect($selectedStoredSessionId.get()).toBe('stored-parent')
    expect(navigate).not.toHaveBeenCalledWith(sessionRoute('branch-stored'))
  })

  it('tags desktop branch sessions as desktop sessions', async () => {
    let createParams: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.create') {
        createParams = params

        return { session_id: 'branch-runtime', stored_session_id: 'branch-stored' } as never
      }

      return {} as never
    })

    setSessions([storedSession({ id: 'stored-parent', message_count: 1 })])
    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: [{ content: 'branch me', role: 'user', timestamp: 1 }],
      session_id: 'stored-parent'
    } as never)

    let branchStoredSession: ((storedSessionId: string) => Promise<boolean>) | null = null
    render(<BranchHarness onReady={branch => (branchStoredSession = branch)} requestGateway={requestGateway} />)
    await waitFor(() => expect(branchStoredSession).not.toBeNull())

    await expect(branchStoredSession!('stored-parent')).resolves.toBe(true)

    expect(createParams).toMatchObject({
      parent_session_id: 'stored-parent',
      source: 'desktop'
    })
  })

  it('branches an open live chat via session.branch with a trimmed message count (bug #1/#3 fix)', async () => {
    let branchParams: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.branch') {
        branchParams = params

        return {
          session_id: 'branch-runtime',
          stored_session_id: 'branch-stored',
          title: 'Branch',
          message_count: 2,
          messages: [],
          info: {}
        } as never
      }

      return {} as never
    })

    setMessages([
      { id: 'q1', role: 'user', parts: [{ type: 'text', text: 'question one' }] },
      { id: 'a1', role: 'assistant', parts: [{ type: 'text', text: 'answer one' }] },
      { id: 'q2', role: 'user', parts: [{ type: 'text', text: 'question two' }] },
      { id: 'a2', role: 'assistant', parts: [{ type: 'text', text: 'answer two' }] }
    ])

    let branchCurrentSession: ((messageId?: string) => Promise<boolean>) | null = null
    render(
      <BranchHarness
        activeSessionId="live-parent"
        onCurrentReady={branch => (branchCurrentSession = branch)}
        onReady={() => undefined}
        requestGateway={requestGateway}
      />
    )
    await waitFor(() => expect(branchCurrentSession).not.toBeNull())

    // Branch from the FIRST assistant reply ("a1"), not the last message �
    // this is exactly the scenario that used to drop the question (bug #1):
    // only the clicked message survived instead of everything up to it.
    await expect(branchCurrentSession!('a1')).resolves.toBe(true)

    expect(requestGateway).toHaveBeenCalledWith('session.branch', {
      session_id: 'live-parent',
      count: 2
    })
    expect(branchParams).toEqual({ session_id: 'live-parent', count: 2 })
  })

  // #67603: right-clicking a session outside the paginated sidebar window is a
  // cache miss. Resolve its owning profile (cache → active → cross-profile) and
  // swap to it before reading the transcript / creating the branch, so the fork
  // is not created on whichever profile happens to be live.
  it('resolves and swaps to the parent profile when the branched session is not cached', async () => {
    setSessions([])
    vi.mocked(getSession).mockResolvedValue(storedSession({ id: 'stored-parent', message_count: 1, profile: 'work' }))
    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: [{ content: 'branch me', role: 'user', timestamp: 1 }],
      session_id: 'stored-parent'
    } as never)

    let createParams: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.create') {
        createParams = params

        return { session_id: 'branch-runtime', stored_session_id: 'branch-stored' } as never
      }

      return {} as never
    })

    let branchStoredSession: ((storedSessionId: string, sessionProfile?: string | null) => Promise<boolean>) | null =
      null

    render(<BranchHarness onReady={branch => (branchStoredSession = branch)} requestGateway={requestGateway} />)
    await waitFor(() => expect(branchStoredSession).not.toBeNull())

    await expect(branchStoredSession!('stored-parent')).resolves.toBe(true)

    expect(ensureGatewayProfile).toHaveBeenCalledWith('work')
    expect(getSessionMessages).toHaveBeenCalledWith('stored-parent', 'work')
    // The create itself must carry the owning profile: in app-global remote
    // mode the soft gateway swap alone is not enough — an omitted profile
    // lands the branch on the launch (default) profile's state.db.
    expect(createParams).toMatchObject({ parent_session_id: 'stored-parent', profile: 'work' })

    vi.mocked(getSession).mockReset()
  })

  it('creates the branch on the cached parent session profile', async () => {
    setSessions([storedSession({ id: 'stored-parent', message_count: 1, profile: 'work' })])
    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: [{ content: 'branch me', role: 'user', timestamp: 1 }],
      session_id: 'stored-parent'
    } as never)

    let createParams: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.create') {
        createParams = params

        return { session_id: 'branch-runtime', stored_session_id: 'branch-stored' } as never
      }

      return {} as never
    })

    let branchStoredSession: ((storedSessionId: string) => Promise<boolean>) | null = null
    render(<BranchHarness onReady={branch => (branchStoredSession = branch)} requestGateway={requestGateway} />)
    await waitFor(() => expect(branchStoredSession).not.toBeNull())

    await expect(branchStoredSession!('stored-parent')).resolves.toBe(true)

    expect(ensureGatewayProfile).toHaveBeenCalledWith('work')
    expect(createParams).toMatchObject({ profile: 'work' })
  })

  it('omits profile for a profile-less parent so single-profile users are unchanged', async () => {
    setSessions([storedSession({ id: 'stored-parent', message_count: 1 })])
    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: [{ content: 'branch me', role: 'user', timestamp: 1 }],
      session_id: 'stored-parent'
    } as never)

    let createParams: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.create') {
        createParams = params

        return { session_id: 'branch-runtime', stored_session_id: 'branch-stored' } as never
      }

      return {} as never
    })

    let branchStoredSession: ((storedSessionId: string) => Promise<boolean>) | null = null
    render(<BranchHarness onReady={branch => (branchStoredSession = branch)} requestGateway={requestGateway} />)
    await waitFor(() => expect(branchStoredSession).not.toBeNull())

    await expect(branchStoredSession!('stored-parent')).resolves.toBe(true)

    expect(createParams).toBeDefined()
    expect(createParams).not.toHaveProperty('profile')
  })
})

// ── Main/tile dedup (the "same session open in main AND its own tab" bug) ─────
// A session is EITHER the main thread OR a tile, never both. openSessionTile
// enforces this from the tile side; resumeSession enforces it from the main
// side by dropping an existing tile when the session loads into main (cold-start
// restore, a pasted/⌘K route, a notification jump), so it can't render twice.
describe('resumeSession drops a redundant tile when the session loads into main', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setResumeFailedSessionId(null)
    setMessages([])
    setSessions([])
    $sessionTiles.set([])
    vi.restoreAllMocks()
  })

  it('closes the tile so the session is not open in both main and its own tab', async () => {
    // The session is already an open tile (e.g. persisted across a restart)...
    $sessionTiles.set([{ storedSessionId: 'stored-1' }])

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        return { session_id: 'runtime-1', resumed: params?.session_id, messages: [], info: {} } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [] } as never)

    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null
    render(<ResumeHarness onReady={r => (resume = r)} requestGateway={requestGateway} />)
    await waitFor(() => expect(resume).not.toBeNull())

    // ...and now it loads into main.
    await resume!('stored-1', true)

    // Its tile is gone — main owns the session, so it renders exactly once.
    expect($sessionTiles.get().some(t => t.storedSessionId === 'stored-1')).toBe(false)
    expect($selectedStoredSessionId.get()).toBe('stored-1')
  })

  it('leaves OTHER sessions tiles untouched', async () => {
    $sessionTiles.set([{ storedSessionId: 'stored-1' }, { storedSessionId: 'stored-2' }])

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        return { session_id: 'runtime-1', resumed: params?.session_id, messages: [], info: {} } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [] } as never)

    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null
    render(<ResumeHarness onReady={r => (resume = r)} requestGateway={requestGateway} />)
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-1', true)

    // Only the resumed session's tile closes; the sibling tile stays put.
    expect($sessionTiles.get().map(t => t.storedSessionId)).toEqual(['stored-2'])
  })
})

// ── Warm-cache mapping integrity (the "open chat A, chat B loads" bug) ─────────
// resumeSession's warm fast-path maps storedSessionId -> runtimeId -> cached
// state. A reaped/respawned pooled backend re-mints runtime ids, so a recycled
// id can resolve to a live-but-DIFFERENT session's cache entry. The fast-path
// must verify the cached state still BELONGS to the resumed session before it
// paints, or it shows a totally different thread under the current route.
const clientState = (storedSessionId: string | null): ClientSessionState => createClientSessionState(storedSessionId)

describe('resumeSession warm-cache mapping integrity', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setResumeFailedSessionId(null)
    setMessages([])
    setSessions([])
    vi.restoreAllMocks()
  })

  it('rejects a cross-wired runtime mapping and falls through to a full resume', async () => {
    // A recycled runtime id ('rt-recycled') is mapped to 'stored-A', but its
    // cached state actually belongs to a DIFFERENT session ('stored-B') — the
    // exact "open chat A, chat B loads" corruption a reaped/respawned pooled
    // backend can leave behind.
    const runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>> = {
      current: new Map([['stored-A', 'rt-recycled']])
    }

    const sessionStateByRuntimeIdRef: MutableRefObject<Map<string, ClientSessionState>> = {
      current: new Map([['rt-recycled', clientState('stored-B')]])
    }

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        return { session_id: 'rt-A-fresh', resumed: params?.session_id, messages: [], info: {} } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [] } as never)

    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null
    render(
      <ResumeHarness
        onReady={r => (resume = r)}
        requestGateway={requestGateway}
        runtimeIdByStoredSessionIdRef={runtimeIdByStoredSessionIdRef}
        sessionStateByRuntimeIdRef={sessionStateByRuntimeIdRef}
      />
    )
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-A', true)

    // The fast-path did NOT short-circuit on the cross-wired cache — the full
    // resume RPC ran, for the session that was actually requested.
    const resumeCalls = requestGateway.mock.calls.filter(([method]) => method === 'session.resume')
    expect(resumeCalls.length).toBe(1)
    expect(resumeCalls[0][1]).toMatchObject({ session_id: 'stored-A' })

    // The corrupt mapping was purged so it can't mis-resolve again.
    expect(runtimeIdByStoredSessionIdRef.current.has('stored-A')).toBe(false)
    expect(sessionStateByRuntimeIdRef.current.has('rt-recycled')).toBe(false)
  })

  it('honours a warm cache entry whose stored id matches and refreshes its persisted transcript', async () => {
    // Correctly-wired mapping: 'rt-A' <-> 'stored-A'. The fast-path should trust
    // it and never reach session.resume. session.activate refreshes the live
    // projection and, critically, rebinds its event transport after reconnect.
    const runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>> = {
      current: new Map([['stored-A', 'rt-A']])
    }

    const sessionStateByRuntimeIdRef: MutableRefObject<Map<string, ClientSessionState>> = {
      current: new Map([['rt-A', clientState('stored-A')]])
    }

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.activate') {
        return {
          session_id: 'rt-A',
          session_key: 'stored-A',
          resumed: 'stored-A',
          message_count: 0,
          messages: [],
          running: false,
          info: {}
        } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [], session_id: 'stored-A' } as never)

    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null
    render(
      <ResumeHarness
        onReady={r => (resume = r)}
        requestGateway={requestGateway}
        runtimeIdByStoredSessionIdRef={runtimeIdByStoredSessionIdRef}
        sessionStateByRuntimeIdRef={sessionStateByRuntimeIdRef}
      />
    )
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-A', true)

    // Fast-path served the session from cache: no full resume RPC, mapping intact.
    // The persisted transcript still refreshes in parallel because the runtime
    // projection can differ even when its row count matches.
    const methods = requestGateway.mock.calls.map(([method]) => method)
    expect(methods).toContain('session.activate')
    expect(methods).not.toContain('session.resume')
    expect(getSessionMessages).toHaveBeenCalledWith('stored-A', undefined)
    expect(requestGateway).toHaveBeenCalledWith(
      'session.activate',
      expect.objectContaining({ omit_messages: true, session_id: 'rt-A' })
    )
    expect(runtimeIdByStoredSessionIdRef.current.get('stored-A')).toBe('rt-A')
  })

  it('preserves cached image attachments through an idle persisted transcript refresh', async () => {
    const runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>> = {
      current: new Map([['stored-A', 'rt-A']])
    }

    const state = clientState('stored-A')
    state.messages = [
      {
        id: 'cached-user',
        role: 'user',
        parts: [{ type: 'text', text: 'describe this image' }],
        attachmentRefs: ['@image:/tmp/photo.png']
      },
      {
        id: 'cached-assistant',
        role: 'assistant',
        parts: [{ type: 'text', text: 'It is a photo.' }]
      }
    ]

    const sessionStateByRuntimeIdRef: MutableRefObject<Map<string, ClientSessionState>> = {
      current: new Map([['rt-A', state]])
    }

    const persistedMessages = [
      { content: 'describe this image', role: 'user', timestamp: 1 },
      { content: 'It is a photo.', role: 'assistant', timestamp: 2 }
    ]

    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: persistedMessages,
      session_id: 'stored-A'
    } as never)

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.activate') {
        return {
          session_id: 'rt-A',
          session_key: 'stored-A',
          resumed: 'stored-A',
          message_count: persistedMessages.length,
          messages: persistedMessages,
          running: false,
          info: {}
        } as never
      }

      return {} as never
    })

    let resumedState: ClientSessionState | undefined
    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null

    render(
      <ResumeHarness
        onReady={ready => (resume = ready)}
        onStateUpdate={(_sessionId, next) => (resumedState = next)}
        requestGateway={requestGateway}
        runtimeIdByStoredSessionIdRef={runtimeIdByStoredSessionIdRef}
        sessionStateByRuntimeIdRef={sessionStateByRuntimeIdRef}
      />
    )
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-A', true)

    expect(requestGateway.mock.calls.map(([method]) => method)).toContain('session.activate')
    expect(getSessionMessages).toHaveBeenCalledWith('stored-A', undefined)
    expect(resumedState?.messages[0]?.attachmentRefs).toEqual(['@image:/tmp/photo.png'])
  })

  it('repairs an idle warm cache from a divergent equal-length persisted transcript', async () => {
    const runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>> = {
      current: new Map([['stored-A', 'rt-A']])
    }

    const state = clientState('stored-A')
    state.messages = [
      {
        id: 'cached-user',
        role: 'user',
        parts: [{ type: 'text', text: 'stale runtime prompt' }]
      },
      {
        id: 'cached-assistant',
        role: 'assistant',
        parts: [{ type: 'text', text: 'stale runtime answer' }]
      }
    ]

    const sessionStateByRuntimeIdRef: MutableRefObject<Map<string, ClientSessionState>> = {
      current: new Map([['rt-A', state]])
    }

    const staleRuntimeMessages = [
      { content: 'stale runtime prompt', role: 'user', timestamp: 1 },
      { content: 'stale runtime answer', role: 'assistant', timestamp: 2 }
    ]

    const persistedMessages = [
      { content: 'prompt saved after compression', role: 'user', timestamp: 3 },
      { content: 'answer saved after compression', role: 'assistant', timestamp: 4 }
    ]

    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: persistedMessages,
      session_id: 'stored-A'
    } as never)

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.activate') {
        return {
          session_id: 'rt-A',
          session_key: 'stored-A',
          resumed: 'stored-A',
          message_count: staleRuntimeMessages.length,
          messages: staleRuntimeMessages,
          running: false,
          info: {}
        } as never
      }

      return {} as never
    })

    let resumedState: ClientSessionState | undefined
    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null

    render(
      <ResumeHarness
        onReady={ready => (resume = ready)}
        onStateUpdate={(_sessionId, next) => (resumedState = next)}
        requestGateway={requestGateway}
        runtimeIdByStoredSessionIdRef={runtimeIdByStoredSessionIdRef}
        sessionStateByRuntimeIdRef={sessionStateByRuntimeIdRef}
      />
    )
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-A', true)

    const renderedMessages = JSON.stringify(resumedState?.messages)
    expect(renderedMessages).toContain('prompt saved after compression')
    expect(renderedMessages).toContain('answer saved after compression')
    expect(renderedMessages).not.toContain('stale runtime answer')
  })

  it('keeps a warm runtime and optimistic turn on a transient activation timeout', async () => {
    const runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>> = {
      current: new Map([['stored-A', 'rt-A']])
    }

    const state = clientState('stored-A')
    state.messages = [
      {
        id: 'user-optimistic',
        role: 'user',
        parts: [{ type: 'text', text: 'do not lose me' }]
      }
    ]

    const sessionStateByRuntimeIdRef: MutableRefObject<Map<string, ClientSessionState>> = {
      current: new Map([['rt-A', state]])
    }

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.activate') {
        throw new Error('request timed out: session.activate')
      }

      return {} as never
    })

    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null
    render(
      <ResumeHarness
        onReady={r => (resume = r)}
        requestGateway={requestGateway}
        runtimeIdByStoredSessionIdRef={runtimeIdByStoredSessionIdRef}
        sessionStateByRuntimeIdRef={sessionStateByRuntimeIdRef}
      />
    )
    await waitFor(() => expect(resume).not.toBeNull())
    await resume!('stored-A', true)

    expect(requestGateway.mock.calls.map(([method]) => method)).not.toContain('session.resume')
    expect(runtimeIdByStoredSessionIdRef.current.get('stored-A')).toBe('rt-A')
    expect(sessionStateByRuntimeIdRef.current.get('rt-A')?.messages[0]?.id).toBe('user-optimistic')
  })
})

describe('createBackendSessionForSend workspace target', () => {
  afterEach(() => {
    cleanup()
    $newChatProfile.set(null)
    $activeGatewayProfile.set('default')
    setCurrentCwd('')
    setNewChatWorkspaceTarget(undefined)
    vi.restoreAllMocks()
  })

  it('omits cwd for an explicit no-workspace draft even when global cwd changes before send', async () => {
    const params = await createWith(
      () => {
        $activeGatewayProfile.set('default')
      },
      handle => {
        handle.startFreshSessionDraft({ workspaceTarget: null })
        $currentCwd.set('/project-open-in-file-browser')
      }
    )

    expect(params).not.toHaveProperty('cwd')
    expect($newChatWorkspaceTarget.get()).toBeUndefined()
  })

  it('uses the clicked workspace target instead of a later global cwd value', async () => {
    const params = await createWith(
      () => {
        $activeGatewayProfile.set('default')
      },
      handle => {
        handle.startFreshSessionDraft({ workspaceTarget: '/clicked-workspace' })
        $currentCwd.set('/project-open-in-file-browser')
      }
    )

    expect(params).toMatchObject({ cwd: '/clicked-workspace' })
  })
})
describe('selectSidebarItem', () => {
  it('fronts the workspace pane when navigating to a sidebar route (issue #72602)', async () => {
    const navigate = vi.fn()
    const requestGateway = vi.fn(async () => ({}) as never)
    let handle: HarnessHandle | null = null

    render(<Harness navigate={navigate} onReady={value => (handle = value)} requestGateway={requestGateway} />)
    await waitFor(() => expect(handle).not.toBeNull())

    act(() => {
      handle!.selectSidebarItem({ icon: (() => null) as never, id: 'skills', label: 'Capabilities', route: '/skills' })
    })

    expect(navigate).toHaveBeenCalledWith('/skills', undefined)
    expect(noteActiveTreeGroup).toHaveBeenCalledWith(null)
    expect(revealTreePane).toHaveBeenCalledWith('workspace')
  })
})

// ── Workspace cwd ownership across a conversation switch (#71254) ─────────────
// resumeSession publishes the new stored id synchronously, but the new
// conversation's cwd only lands when session.resume settles. In that window
// $currentCwd still holds the PREVIOUS conversation's folder, so anything that
// derives from the workspace (the coding rail's Git probe) republishes the old
// repo's facts under the newly selected chat.
//
// The fix is ownership, not emptiness: $currentCwd is deliberately NEVER cleared
// on a switch (clearing collapses the workspace/review panes and drops file-tree
// state), it is merely marked as not-yet-owned so the primary workspace slices
// hide. These tests
// pin the ownership TRANSITIONS at the resume boundary — entry, settle, warm
// switch, and fresh draft — because that is where the window opens and closes.
describe('resumeSession workspace cwd ownership', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setResumeFailedSessionId(null)
    setSelectedStoredSessionId(null)
    setMessages([])
    setSessions([])
    setCurrentBranch('')
    // Clears the persisted workspace key too (setCurrentCwd removes it when
    // blank), so a remembered path can't leak into another test's assertions.
    setCurrentCwd('')
    setWorkspaceCwdOwner(null)
    vi.restoreAllMocks()
  })

  function mountResume(
    requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>,
    options: {
      runtimeIdByStoredSessionIdRef?: MutableRefObject<Map<string, string>>
      selectedStoredSessionId?: null | string
      sessionStateByRuntimeIdRef?: MutableRefObject<Map<string, ClientSessionState>>
    } = {}
  ): Promise<(storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>> {
    let resume: ((storedSessionId: string, replaceRoute?: boolean) => Promise<unknown>) | null = null
    render(<ResumeHarness onReady={r => (resume = r)} requestGateway={requestGateway} {...options} />)

    return waitFor(() => {
      expect(resume).not.toBeNull()

      return resume!
    })
  }

  it('claims the stored row workspace at resume entry, before session.resume settles', async () => {
    // Switching from a chat in /repo-a into a chat whose stored row already
    // records /repo-b. The seed + claim must happen at ENTRY: waiting for the
    // RPC is exactly the window #71254 lives in.
    setCurrentCwd('/repo-a')
    setSessions([storedSession({ cwd: '/repo-b' })])

    const resumeGate = deferred<Record<string, unknown>>()
    let resumeRpcIssued = false

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        resumeRpcIssued = true

        return (await resumeGate.promise) as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [], session_id: 'stored-1' } as never)

    const resume = await mountResume(requestGateway)
    const resumePromise = resume('stored-1', true)

    // The RPC is in flight and unresolved — the intermediate state the coding
    // rail sees on a switch.
    await waitFor(() => expect(resumeRpcIssued).toBe(true))
    expect($selectedStoredSessionId.get()).toBe('stored-1')
    expect($currentCwd.get()).toBe('/repo-b')
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)

    resumeGate.resolve({
      session_id: 'runtime-1',
      session_key: 'stored-1',
      resumed: 'stored-1',
      message_count: 0,
      messages: [],
      info: { cwd: '/repo-b', branch: 'main' }
    })
    await resumePromise

    // The settle confirms the same workspace, so ownership stays claimed.
    expect($currentCwd.get()).toBe('/repo-b')
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })

  it('withholds ownership for a stored row with no cwd instead of adopting the previous folder', async () => {
    // The COMMON switch target: a bare new chat is detached by design, so its
    // row carries cwd: null. The path on screen is still /repo-a, and it is not
    // this conversation's — so it stays visible (clearing would collapse the
    // panes) but must read as unowned.
    //
    // Entering with a claim ALREADY standing for this same id is the state a
    // re-entered resume arrives in (use-route-resume's retry, a reconnect, a
    // reselect), while $currentCwd has meanwhile moved to another conversation's
    // folder. Ownership therefore has to be actively RELEASED here — merely
    // "not claiming" would leave the stale claim asserting that /repo-a is this
    // conversation's workspace.
    setSelectedStoredSessionId('stored-1')
    setWorkspaceCwdOwner('stored-1')
    setCurrentCwd('/repo-a')
    setCurrentBranch('feature-a')
    setSessions([storedSession({ cwd: null })])

    const resumeGate = deferred<Record<string, unknown>>()
    let resumeRpcIssued = false

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        resumeRpcIssued = true

        return (await resumeGate.promise) as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [], session_id: 'stored-1' } as never)

    const resume = await mountResume(requestGateway, { selectedStoredSessionId: 'stored-1' })
    const resumePromise = resume('stored-1', true)

    await waitFor(() => expect(resumeRpcIssued).toBe(true))
    // The previous conversation's path is deliberately preserved...
    expect($currentCwd.get()).toBe('/repo-a')
    // ...but the newly selected conversation does not own it.
    expect(workspaceCwdBelongsToSelectedSession()).toBe(false)
    // The branch label is derived from the workspace, so it cannot carry over.
    expect($currentBranch.get()).toBe('')

    resumeGate.resolve({
      session_id: 'runtime-1',
      session_key: 'stored-1',
      resumed: 'stored-1',
      message_count: 0,
      messages: [],
      info: {}
    })
    await resumePromise

    // A settled report that still has no cwd is not evidence of ownership.
    expect(workspaceCwdBelongsToSelectedSession()).toBe(false)
    expect($currentCwd.get()).toBe('/repo-a')
  })

  it('leaves the remembered workspace untouched when switching through a cwd-less session', async () => {
    // Releasing ownership must not write cwd: the per-connection remembered
    // workspace is what a relaunch restores, so a switch through a detached chat
    // cannot be allowed to blank or rewrite it.
    setCurrentCwd('/repo-a')
    const rememberedBefore = getRememberedWorkspaceCwd()
    setSessions([storedSession({ cwd: null })])

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        return {
          session_id: 'runtime-1',
          session_key: 'stored-1',
          resumed: params?.session_id,
          message_count: 0,
          messages: [],
          info: {}
        } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [], session_id: 'stored-1' } as never)

    const resume = await mountResume(requestGateway)
    await resume('stored-1', true)

    expect(rememberedBefore).toBe('/repo-a')
    expect(getRememberedWorkspaceCwd()).toBe(rememberedBefore)
  })

  it('claims ownership when the resume settles with an authoritative cwd', async () => {
    // Entry had nothing to seed from (cwd-less row), so ownership was released;
    // the settled runtime report is what hands it back. Without this the rail
    // would stay hidden for the rest of the session.
    setCurrentCwd('/repo-a')
    setSessions([storedSession({ cwd: null })])

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'session.resume') {
        return {
          session_id: 'runtime-1',
          session_key: 'stored-1',
          resumed: params?.session_id,
          message_count: 0,
          messages: [],
          info: { cwd: '/repo-c', branch: 'trunk' }
        } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [], session_id: 'stored-1' } as never)

    const resume = await mountResume(requestGateway)
    await resume('stored-1', true)

    expect($currentCwd.get()).toBe('/repo-c')
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })

  it('claims ownership on a warm switch even when session.activate is unavailable', async () => {
    // The warm fast-path's compat branch returns BEFORE applyRuntimeInfo would
    // run, so an old backend without session.activate must not leave the switch
    // permanently un-re-homed. The warm cache IS this conversation's own
    // workspace truth, so the claim belongs next to the cwd write.
    setCurrentCwd('/repo-a')
    setWorkspaceCwdOwner('stored-previous')
    setSessions([storedSession({ id: 'stored-A', cwd: '/repo-warm' })])

    const runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>> = {
      current: new Map([['stored-A', 'rt-A']])
    }

    const warmState = clientState('stored-A')
    warmState.cwd = '/repo-warm'
    warmState.branch = 'warm-branch'

    const sessionStateByRuntimeIdRef: MutableRefObject<Map<string, ClientSessionState>> = {
      current: new Map([['rt-A', warmState]])
    }

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.activate') {
        // Shape isMissingRpcMethod recognizes as "backend predates the method".
        throw new Error('Method not found: session.activate')
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({ messages: [], session_id: 'stored-A' } as never)

    const resume = await mountResume(requestGateway, {
      runtimeIdByStoredSessionIdRef,
      sessionStateByRuntimeIdRef
    })

    await resume('stored-A', true)

    // The compat branch swallowed the missing method (it fell back to
    // session.usage) rather than throwing the switch away...
    expect(requestGateway.mock.calls.map(([method]) => method)).toContain('session.usage')
    // ...and the workspace is re-homed onto the session that was opened.
    expect($currentCwd.get()).toBe('/repo-warm')
    expect($selectedStoredSessionId.get()).toBe('stored-A')
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })

  it('lets a fresh draft own its own workspace', async () => {
    // A draft resolves its workspace synchronously, so there is no switch window
    // to protect: selected id and owner are both null, which MATCH — the rail
    // stays live on a new chat instead of withholding forever.
    setSelectedStoredSessionId('stored-previous')
    setWorkspaceCwdOwner('stored-previous')

    const requestGateway = vi.fn(async () => ({}) as never)
    let handle: HarnessHandle | null = null

    render(<Harness onReady={value => (handle = value)} requestGateway={requestGateway} />)
    await waitFor(() => expect(handle).not.toBeNull())

    act(() => handle!.startFreshSessionDraft({ preserveRoute: true }))

    expect($selectedStoredSessionId.get()).toBeNull()
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })
})

// ── Workspace cwd ownership across a compression id rotation (#71254) ─────────
// A rotation is a RENAME of the conversation the user is already looking at, not
// a switch to another one: the folder is unchanged and the only thing that moved
// is the stored id. If ownership keeps naming the rotated-out id while selection
// moves to the tip, the mismatch is permanent and cannot self-heal — $currentCwd
// never changes (so its subscription never fires), the owner is never written
// again, and every primary read still sees a mismatch. The coding rail and
// worktree menu then stay blank for the life of the session, which is worse than
// the stale-facts bug ownership fixes.
describe('stored-id rotation workspace cwd ownership', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setActiveSessionStoredIdRotation(null)
    setSelectedStoredSessionId(null)
    setSessions([])
    // setCurrentCwd('') also drops the persisted workspace key, so the seeded
    // path cannot leak into another test's assertions.
    setCurrentCwd('')
    setWorkspaceCwdOwner(null)
    vi.restoreAllMocks()
  })

  it('keeps the workspace owned by the conversation after its stored id rotates', async () => {
    const tipBefore = 'stored-A'
    const tipAfter = 'stored-A-next'
    const runtimeSessionId = 'runtime-A'
    const activeSessionIdRef: MutableRefObject<string | null> = { current: runtimeSessionId }
    const selectedStoredSessionIdRef: MutableRefObject<string | null> = { current: tipBefore }
    const navigate = vi.fn()

    // The realistic pre-rotation state: this conversation resumed into /repo-a
    // and legitimately owns it, so probes are running.
    setSessions([storedSession({ _lineage_root_id: tipBefore, id: tipAfter })])
    setSelectedStoredSessionId(tipBefore)
    setActiveSessionId(runtimeSessionId)
    setCurrentCwd('/repo-a')
    setWorkspaceCwdOwner(tipBefore)
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)

    render(
      <StoredIdRotationHarness
        activeSessionIdRef={activeSessionIdRef}
        getRoutedStoredSessionId={() => tipBefore}
        navigate={navigate}
        selectedStoredSessionIdRef={selectedStoredSessionIdRef}
      />
    )

    act(() => {
      setActiveSessionStoredIdRotation({
        nextStoredSessionId: tipAfter,
        previousStoredSessionId: tipBefore,
        runtimeSessionId
      })
    })

    await waitFor(() => expect($selectedStoredSessionId.get()).toBe(tipAfter))

    // Same conversation, same folder — the rotation must not move the workspace.
    expect($currentCwd.get()).toBe('/repo-a')
    // Ownership followed the rename, so the primary visibility gate stays open.
    // Asserting the predicate (with the rotated selection above) is what pins
    // owner === tip.
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })
})

// ── Workspace cwd ownership when a draft becomes a real conversation (#71254) ─
// The first message on a fresh draft moves the selection from null (the draft)
// onto a real stored id. Ownership has to move with it: the draft's folder IS
// the new conversation's workspace. Claiming it only indirectly — via
// applyRuntimeInfo(created.info) — is not enough, because `info` is OPTIONAL on
// SessionCreateResponse and applyRuntimeInfo returns early for undefined. A
// backend whose session.create omits `info` therefore left the marker on the
// draft (null) while the selection named a real conversation, and that mismatch
// does not self-heal: $currentCwd never moves (so its subscription never fires)
// and the owner is never written again, so every later primary read still sees
// the mismatch — a permanently blank coding rail rather than a brief pause.
describe('createBackendSessionForSend workspace cwd ownership', () => {
  afterEach(() => {
    cleanup()
    $newChatProfile.set(null)
    $activeGatewayProfile.set('default')
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
    setMessages([])
    setSessions([])
    // setCurrentCwd('') also drops the persisted workspace key, so the seeded
    // draft path cannot leak into another test's assertions.
    setCurrentCwd('')
    setWorkspaceCwdOwner(null)
    setNewChatWorkspaceTarget(undefined)
    vi.restoreAllMocks()
  })

  it('claims the workspace for the created conversation when session.create omits info', async () => {
    // Realistic draft pre-state: nothing is selected, nobody owns the path, and
    // the composer is sitting in a folder — the draft owns its own workspace
    // (null === null), so the rail is live before the send.
    setSelectedStoredSessionId(null)
    setWorkspaceCwdOwner(null)
    setCurrentCwd('/repo-draft')
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.create') {
        // NO `info` key — the shape an older backend answers with, and the one
        // that left ownership behind on the draft.
        return { session_id: RUNTIME_SESSION_ID, stored_session_id: 'stored-created' } as never
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    render(<Harness onReady={value => (handle = value)} requestGateway={requestGateway} />)
    await waitFor(() => expect(handle).not.toBeNull())

    await act(async () => {
      await handle!.createBackendSessionForSend('first message')
    })

    // The selection moved onto the real conversation...
    expect($selectedStoredSessionId.get()).toBe('stored-created')
    // ...the draft's folder carried over unchanged (this is the same workspace,
    // not a switch)...
    expect($currentCwd.get()).toBe('/repo-draft')
    // ...and the conversation now on screen owns it, so the coding rail is
    // visible instead of remaining blank.
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })
})

// ── Workspace cwd ownership on a failed-delete rollback (#71254) ──────────────
// Deleting the selected conversation optimistically tears it down via
// startFreshSessionDraft(true), which hands ownership to the draft (null). When
// the session.delete RPC then FAILS, the catch block puts the selection back —
// and it has to put the owner back with it. Otherwise the restored conversation
// is selected while the marker still names the draft, which is a permanent
// mismatch: the primary repo/worktree slices remain hidden, and nothing on this
// path restores ownership.
// use-route-resume is not a backstop here — its gate is
// (gatewayBecameOpen || !alreadyActive) && shouldResume, and the failure branch
// restores activeSessionIdRef while leaving runtimeIdByStoredSessionIdRef
// intact, so alreadyActive can be true and no resume re-claims the workspace.
function RemoveSessionHarness({
  activeSessionId,
  navigate = vi.fn(),
  onReady,
  requestGateway,
  runtimeIdByStoredSessionIdRef,
  selectedStoredSessionId
}: {
  activeSessionId: null | string
  navigate?: ReturnType<typeof vi.fn>
  onReady: (removeSession: (storedSessionId: string) => Promise<void>) => void
  requestGateway: <T>(method: string, params?: Record<string, unknown>) => Promise<T>
  runtimeIdByStoredSessionIdRef?: MutableRefObject<Map<string, string>>
  selectedStoredSessionId: null | string
}) {
  const ref = <T,>(value: T): MutableRefObject<T> => ({ current: value })

  const actions = useSessionActions({
    activeSessionId,
    activeSessionIdRef: ref<string | null>(activeSessionId),
    busyRef: ref(false),
    creatingSessionRef: ref(false),
    ensureSessionState: () => ({}) as ClientSessionState,
    getRouteToken: () => 'token',
    getRoutedStoredSessionId: () => selectedStoredSessionId,
    navigate: navigate as never,
    requestGateway,
    resetViewSync: vi.fn(),
    runtimeIdByStoredSessionIdRef: runtimeIdByStoredSessionIdRef ?? ref(new Map<string, string>()),
    selectedStoredSessionId,
    selectedStoredSessionIdRef: ref<string | null>(selectedStoredSessionId),
    sessionStateByRuntimeIdRef: ref(new Map<string, ClientSessionState>()),
    syncSessionStateToView: vi.fn(),
    updateSessionState: () => ({}) as ClientSessionState
  })

  useEffect(() => {
    onReady(actions.removeSession)
  }, [actions.removeSession, onReady])

  return null
}

describe('removeSession workspace cwd ownership on delete failure', () => {
  afterEach(() => {
    cleanup()
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
    setMessages([])
    setSessions([])
    // setCurrentCwd('') also drops the persisted workspace key, so the seeded
    // path cannot leak into another test's assertions.
    setCurrentCwd('')
    setWorkspaceCwdOwner(null)
    vi.restoreAllMocks()
  })

  it('restores workspace ownership with the selection when session.delete rejects', async () => {
    // The doomed conversation is the open one and legitimately owns /repo-doomed,
    // so primary workspace state is visible before the delete.
    setSessions([storedSession({ cwd: '/repo-doomed', id: 'stored-doomed' })])
    setSelectedStoredSessionId('stored-doomed')
    setCurrentCwd('/repo-doomed')
    setWorkspaceCwdOwner('stored-doomed')
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)

    // `@/hermes` is mocked at the top of this file, so its deleteSession is
    // already a vi.fn(); make this delete reject to take the rollback branch.
    const { deleteSession } = await import('@/hermes')
    vi.mocked(deleteSession).mockRejectedValue(new Error('state.db is read-only'))

    const requestGateway = vi.fn(async () => ({}) as never)
    let removeSession: ((storedSessionId: string) => Promise<void>) | null = null

    render(
      <RemoveSessionHarness
        activeSessionId={null}
        onReady={remove => (removeSession = remove)}
        requestGateway={requestGateway}
        selectedStoredSessionId="stored-doomed"
      />
    )
    await waitFor(() => expect(removeSession).not.toBeNull())

    await act(async () => {
      await removeSession!('stored-doomed')
    })

    // The rollback branch ran: startFreshSessionDraft had cleared the selection
    // to null, and only the failure path puts it back.
    expect(deleteSession).toHaveBeenCalledWith('stored-doomed', undefined)
    expect($selectedStoredSessionId.get()).toBe('stored-doomed')
    // And the conversation that is selected again owns the workspace. Left on
    // the draft's null, the mismatch is permanent and the rail stays blank for
    // the rest of the session.
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })
})

// ── Workspace cwd ownership across a split tile / create (#71254) ─────────────
describe('openNewSessionTile workspace cwd ownership', () => {
  afterEach(() => {
    cleanup()
    setSessions([])
    $sessionTiles.set([])
    setSelectedStoredSessionId(null)
    setCurrentCwd('')
    setWorkspaceCwdOwner(null)
    vi.restoreAllMocks()
  })

  it('does not replace a fresh draft workspace with the new tile workspace', async () => {
    setSelectedStoredSessionId(null)
    setCurrentCwd('/repo-draft')
    setWorkspaceCwdOwner(null)

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.create') {
        return {
          info: { cwd: '/repo-tile' },
          session_id: 'tile-runtime',
          stored_session_id: 'tile-stored'
        } as never
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    render(<Harness onReady={value => (handle = value)} requestGateway={requestGateway} />)
    await waitFor(() => expect(handle).not.toBeNull())

    await act(async () => {
      await handle!.openNewSessionTile('right', { cwd: '/repo-tile-requested' })
    })

    expect($selectedStoredSessionId.get()).toBeNull()
    expect($currentCwd.get()).toBe('/repo-draft')
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })
})

// ── Workspace cwd ownership across a fork / create (#71254) ───────────────────
// A fork deliberately keeps the PARENT selected while minting a child in a
// different repo. Its runtime info must remain background-scoped: the returned
// patch still seeds the child session, but the main workspace and ownership stay
// with the parent.
describe('forkBranch workspace cwd ownership', () => {
  afterEach(() => {
    cleanup()
    setSessions([])
    $sessionTiles.set([])
    setSelectedStoredSessionId(null)
    // setCurrentCwd('') also drops the persisted workspace key, so the seeded
    // path cannot leak into another test's assertions.
    setCurrentCwd('')
    setWorkspaceCwdOwner(null)
    vi.restoreAllMocks()
  })

  it('does not claim the branch workspace for the parent left selected', async () => {
    // The open chat reports no workspace of its own, so ownership is released and
    // the visible path is still some earlier conversation's folder — the state
    // every switch through a detached session lands in.
    setSessions([
      storedSession({ cwd: null, id: 'stored-parent', message_count: 1 }),
      storedSession({ cwd: '/repo-branch', id: 'stored-source', message_count: 2 })
    ])
    setSelectedStoredSessionId('stored-parent')
    setCurrentCwd('/repo-previous')
    releaseWorkspaceCwdOwner()
    const ownerBeforeFork = $workspaceCwdOwner.get()
    expect(workspaceCwdBelongsToSelectedSession()).toBe(false)

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.create') {
        // The branch runs in ITS parent row's repo, which is not the folder the
        // selected conversation is looking at.
        return {
          info: { cwd: '/repo-branch' },
          session_id: 'branch-runtime',
          stored_session_id: 'branch-stored'
        } as never
      }

      return {} as never
    })

    vi.mocked(getSessionMessages).mockResolvedValue({
      messages: [{ content: 'branch me', role: 'user', timestamp: 1 }],
      session_id: 'stored-source'
    } as never)

    let branchStoredSession: ((storedSessionId: string) => Promise<boolean>) | null = null
    render(<BranchHarness onReady={branch => (branchStoredSession = branch)} requestGateway={requestGateway} />)
    await waitFor(() => expect(branchStoredSession).not.toBeNull())

    await expect(branchStoredSession!('stored-source')).resolves.toBe(true)

    // The fork opened as its own tab and left the parent chat selected...
    expect($selectedStoredSessionId.get()).toBe('stored-parent')
    // ...and the live workspace never moved. A report for a conversation the user
    // is NOT looking at must not touch $currentCwd at all: doing so re-keys the
    // primary rail onto the branch's cached Git facts under the parent chat —
    // #71254 from the other direction.
    expect($currentCwd.get()).toBe('/repo-previous')
    // The path is still recorded where a background session actually needs it:
    // the branch's own row (the applier returns it on the patch, which the fork
    // folds into that conversation's cache), not the live workspace.
    expect($sessions.get().find(session => session.id === 'branch-stored')?.cwd).toBe('/repo-branch')
    // Not touching $currentCwd also keeps the per-connection remembered workspace
    // on the conversation in front of the user rather than persisting a folder
    // they never navigated to.
    expect(getRememberedWorkspaceCwd()).toBe('/repo-previous')
    // The branch's folder is NOT the selected conversation's workspace, and the
    // marker must still say so. Claiming here is what let the parent's coding rail
    // publish the branch repo's Git facts.
    expect($workspaceCwdOwner.get()).toBe(ownerBeforeFork)
    expect(workspaceCwdBelongsToSelectedSession()).toBe(false)
  })
})

// The mirror-image guard for background tile/branch scoping: a create whose
// session genuinely BECOMES the foreground selection must claim its workspace.
// Otherwise the selected id changes while the owner stays on the draft, hiding
// the primary coding rail even though the new conversation kept the same path.
describe('createBackendSessionForSend workspace cwd ownership with runtime info', () => {
  afterEach(() => {
    cleanup()
    $newChatProfile.set(null)
    $activeGatewayProfile.set('default')
    setActiveSessionId(null)
    setSelectedStoredSessionId(null)
    setMessages([])
    setSessions([])
    // setCurrentCwd('') also drops the persisted workspace key, so the seeded
    // draft path cannot leak into another test's assertions.
    setCurrentCwd('')
    setWorkspaceCwdOwner(null)
    setNewChatWorkspaceTarget(undefined)
    vi.restoreAllMocks()
  })

  it('claims the workspace reported by session.create for the created conversation', async () => {
    setSelectedStoredSessionId(null)
    setWorkspaceCwdOwner(null)
    setCurrentCwd('/repo-draft')

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.create') {
        // The backend answers with the workspace it actually resolved (here a
        // canonicalized path), so the claim has to follow `info.cwd`.
        return {
          info: { cwd: '/repo-draft-resolved' },
          session_id: RUNTIME_SESSION_ID,
          stored_session_id: 'stored-created'
        } as never
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    render(<Harness onReady={value => (handle = value)} requestGateway={requestGateway} />)
    await waitFor(() => expect(handle).not.toBeNull())

    await act(async () => {
      await handle!.createBackendSessionForSend('first message')
    })

    expect($selectedStoredSessionId.get()).toBe('stored-created')
    expect($currentCwd.get()).toBe('/repo-draft-resolved')
    // This create's session IS the new selection, so ownership moves with it and
    // the coding rail is visible immediately.
    expect($workspaceCwdOwner.get()).toBe('stored-created')
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
  })
})
