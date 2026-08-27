import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter, useNavigate } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { assistantTextPart, type ChatMessage } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $activeSessionId,
  $awaitingResponse,
  $busy,
  $connection,
  $contextSuggestions,
  $currentCwd,
  $currentModel,
  $currentProvider,
  $freshDraftReady,
  $gatewayState,
  $messages,
  $selectedStoredSessionId,
  $sessions
} from '@/store/session'
import { $sessionStates } from '@/store/session-states'

const threadRenderCount = vi.hoisted(() => ({ current: 0 }))

vi.mock('@/components/assistant-ui/thread', async () => {
  const React = await import('react')

  return {
    Thread: () => {
      threadRenderCount.current += 1

      return React.createElement('div', { 'data-testid': 'thread' })
    }
  }
})

vi.mock('@/components/Backdrop', async () => {
  const React = await import('react')

  return { Backdrop: () => React.createElement('div', { 'data-testid': 'backdrop' }) }
})

vi.mock('@/components/prompt-overlays', () => ({ PromptOverlays: () => null }))
vi.mock('@/components/chat/vibe-hearts', () => ({ COMPOSER_HEART_CONFIG: {}, HeartField: () => null }))
vi.mock('@/lib/model-options', () => ({
  modelOptionsQueryKey: (...parts: unknown[]) => ['model-options', ...parts],
  requestModelOptions: vi.fn(async () => ({ models: [] }))
}))
vi.mock('./chat-drop-overlay', () => ({ ChatDropOverlay: () => null }))
vi.mock('./chat-swap-overlay', () => ({ ChatSwapOverlay: () => null, ChatSyncBadge: () => null }))
vi.mock('./composer', async () => {
  const React = await import('react')

  return {
    ChatBar: ({ actionsDisabled, busy }: { actionsDisabled?: boolean; busy: boolean }) =>
      React.createElement('textarea', {
        'data-actions-disabled': actionsDisabled ? 'true' : 'false',
        'data-busy': busy ? 'true' : 'false',
        'data-testid': 'composer'
      }),
    ChatBarFallback: () => null
  }
})
vi.mock('./hooks/use-file-drop-zone', () => ({
  useFileDropZone: () => ({ dragKind: null, dropHandlers: {} })
}))
vi.mock('./sidebar/session-actions-menu', async () => {
  const React = await import('react')

  return {
    SessionActionsMenu: ({ children }: { children: React.ReactNode }) =>
      React.createElement('div', { 'data-testid': 'session-actions-menu' }, children)
  }
})

const { ChatView, shouldShowChatBar } = await import('./index')

function assistantMessage(id: string, text: string): ChatMessage {
  return {
    id,
    parts: [assistantTextPart(text)],
    role: 'assistant'
  }
}

function chatViewProps() {
  return {
    gateway: null,
    maxVoiceRecordingSeconds: 120,
    onAddContextRef: vi.fn(),
    onAddUrl: vi.fn(),
    onAttachDroppedItems: vi.fn(),
    onAttachImageBlob: vi.fn(),
    onBranchInNewChat: vi.fn(),
    onCancel: vi.fn(),
    onDeleteSelectedSession: vi.fn(),
    onEdit: vi.fn(),
    onPasteClipboardImage: vi.fn(),
    onPickFiles: vi.fn(),
    onPickFolders: vi.fn(),
    onPickImages: vi.fn(),
    onReload: vi.fn(),
    onRemoveAttachment: vi.fn(),
    onRetryResume: vi.fn(),
    onSteer: vi.fn(),
    onSubmit: vi.fn(),
    onThreadMessagesChange: vi.fn(),
    onToggleSelectedPin: vi.fn(),
    onTranscribeAudio: vi.fn()
  }
}

describe('shouldShowChatBar', () => {
  it('preserves initial-loading, exhausted, watch, and normal visibility', () => {
    expect(
      shouldShowChatBar({
        activeSessionId: null,
        isRoutedSessionView: true,
        messagesEmpty: true,
        resumeExhausted: false,
        watchWindow: false
      })
    ).toBe(false)
    expect(
      shouldShowChatBar({
        activeSessionId: 'runtime-a',
        isRoutedSessionView: true,
        messagesEmpty: false,
        resumeExhausted: true,
        watchWindow: false
      })
    ).toBe(false)
    expect(
      shouldShowChatBar({
        activeSessionId: 'runtime-a',
        isRoutedSessionView: true,
        messagesEmpty: false,
        resumeExhausted: false,
        watchWindow: true
      })
    ).toBe(false)
    expect(
      shouldShowChatBar({
        activeSessionId: 'runtime-a',
        isRoutedSessionView: true,
        messagesEmpty: false,
        resumeExhausted: false,
        watchWindow: false
      })
    ).toBe(true)
  })
})

describe('ChatView render isolation', () => {
  beforeEach(() => {
    threadRenderCount.current = 0
    $activeSessionId.set('runtime-1')
    $activeGatewayProfile.set('profile-a')
    $awaitingResponse.set(false)
    $busy.set(false)
    $contextSuggestions.set([])
    $currentCwd.set('/work')
    $currentModel.set('test-model')
    $currentProvider.set('test-provider')
    $connection.set({ connectionId: 'local', mode: 'local' } as never)
    $freshDraftReady.set(false)
    $gatewayState.set('closed')
    const initialMessages = [assistantMessage('assistant-1', 'Stable historical answer')]

    $messages.set(initialMessages)
    $selectedStoredSessionId.set('stored-1')
    $sessionStates.set({
      'runtime-1': {
        ...createClientSessionState('stored-1'),
        messages: initialMessages
      }
    })
    $sessions.set([{ id: 'stored-1', message_count: 1, title: 'Stable chat' } as never])
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    $activeSessionId.set(null)
    $activeGatewayProfile.set('default')
    $awaitingResponse.set(false)
    $busy.set(false)
    $contextSuggestions.set([])
    $currentCwd.set('')
    $currentModel.set('')
    $currentProvider.set('')
    $connection.set(null)
    $freshDraftReady.set(false)
    $gatewayState.set('idle')
    $messages.set([])
    $selectedStoredSessionId.set(null)
    $sessionStates.set({})
    $sessions.set([])
  })

  it('does not re-render chat history when an unrelated parent idle tick updates', () => {
    const props = chatViewProps()

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } }
    })

    function ParentTickHarness() {
      const [tick, setTick] = useState(0)

      return (
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={['/stored-1']}>
            <button onClick={() => setTick(value => value + 1)} type="button">
              parent tick {tick}
            </button>
            <ChatView {...props} />
          </MemoryRouter>
        </QueryClientProvider>
      )
    }

    render(<ParentTickHarness />)

    expect(screen.getByTestId('thread')).toBeTruthy()
    expect(threadRenderCount.current).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: /parent tick/i }))

    // memo(ChatView) with stable props must absorb the parent's idle tick —
    // the transcript (Thread) must not re-render. This is PR #38470's contract.
    expect(threadRenderCount.current).toBe(1)
  })

  it('keeps the focused composer mounted but action-blocked through a transient route-session mismatch', () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } }
    })

    const props = chatViewProps()

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/stored-1']}>
          <ChatView {...props} />
        </MemoryRouter>
      </QueryClientProvider>
    )

    const composer = screen.getByTestId('composer') as HTMLTextAreaElement
    composer.value = 'draft in progress'
    composer.focus()
    composer.setSelectionRange(5, 5)

    act(() => {
      $sessions.set([
        { id: 'stored-1', message_count: 1, title: 'Stable chat' } as never,
        { id: 'stored-2', message_count: 1, title: 'Incoming update' } as never
      ])
      $selectedStoredSessionId.set('stored-2')
    })

    const composerAfterMismatch = screen.getByTestId('composer') as HTMLTextAreaElement

    expect(composerAfterMismatch).toBe(composer)
    expect(globalThis.document.activeElement).toBe(composer)
    expect(composerAfterMismatch.value).toBe('draft in progress')
    expect(composerAfterMismatch.selectionStart).toBe(5)
    expect(composerAfterMismatch.selectionEnd).toBe(5)
    expect(composerAfterMismatch.dataset.actionsDisabled).toBe('true')
  })

  it('blocks busy session A actions during a route-first navigation to session B', () => {
    $sessionStates.set({
      'runtime-1': {
        ...createClientSessionState('stored-1'),
        busy: true,
        messages: [assistantMessage('assistant-a', 'A is still running')]
      }
    })
    $sessions.set([
      { id: 'stored-1', message_count: 1, title: 'Busy A' } as never,
      { id: 'stored-2', message_count: 1, title: 'Target B' } as never
    ])

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } }
    })

    const props = chatViewProps()

    function RouteFirstHarness() {
      const navigate = useNavigate()

      return (
        <>
          <button onClick={() => navigate('/stored-2')} type="button">
            Navigate to B
          </button>
          <ChatView {...props} />
        </>
      )
    }

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/stored-1']}>
          <RouteFirstHarness />
        </MemoryRouter>
      </QueryClientProvider>
    )

    const composer = screen.getByTestId('composer') as HTMLTextAreaElement
    expect(composer.dataset.busy).toBe('true')
    expect(composer.dataset.actionsDisabled).toBe('false')
    composer.value = 'draft for B'
    composer.focus()
    composer.setSelectionRange(7, 7)

    fireEvent.click(screen.getByRole('button', { name: 'Navigate to B' }))

    const composerDuringMismatch = screen.getByTestId('composer') as HTMLTextAreaElement

    expect(composerDuringMismatch).toBe(composer)
    expect(composerDuringMismatch.dataset.busy).toBe('true')
    expect(composerDuringMismatch.dataset.actionsDisabled).toBe('true')
    expect(globalThis.document.activeElement).toBe(composer)

    // Selected B can publish before runtime A is replaced. Route-vs-selected is
    // now clean, but the active runtime still owns busy/session actions for A —
    // the fence must remain until runtime identity converges too.
    act(() => $selectedStoredSessionId.set('stored-2'))

    const composerAfterSelectionConverges = screen.getByTestId('composer') as HTMLTextAreaElement

    expect(composerAfterSelectionConverges).toBe(composer)
    expect(composerAfterSelectionConverges.dataset.busy).toBe('true')
    expect(composerAfterSelectionConverges.dataset.actionsDisabled).toBe('true')

    act(() => {
      $sessionStates.set({
        ...$sessionStates.get(),
        'runtime-2': {
          ...createClientSessionState('stored-2'),
          messages: [assistantMessage('assistant-b', 'B is ready')]
        }
      })
      $activeSessionId.set('runtime-2')
    })

    const composerAfterRuntimeConverges = screen.getByTestId('composer') as HTMLTextAreaElement

    expect(composerAfterRuntimeConverges).toBe(composer)
    expect(composerAfterRuntimeConverges.dataset.busy).toBe('false')
    expect(composerAfterRuntimeConverges.dataset.actionsDisabled).toBe('false')
  })

  it('fences a route-first navigation from session A to New Chat until A is cleared', () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } }
    })

    const props = chatViewProps()

    function NewChatHarness() {
      const navigate = useNavigate()

      return (
        <>
          <button onClick={() => navigate('/')} type="button">
            Navigate to New Chat
          </button>
          <ChatView {...props} />
        </>
      )
    }

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/stored-1']}>
          <NewChatHarness />
        </MemoryRouter>
      </QueryClientProvider>
    )

    const composer = screen.getByTestId('composer') as HTMLTextAreaElement

    expect(composer.dataset.actionsDisabled).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'Navigate to New Chat' }))

    const transitioningComposer = screen.getByTestId('composer') as HTMLTextAreaElement

    expect(transitioningComposer).toBe(composer)
    expect(transitioningComposer.dataset.actionsDisabled).toBe('true')

    act(() => {
      $selectedStoredSessionId.set(null)
      $activeSessionId.set(null)
      $sessionStates.set({})
      $messages.set([])
    })

    expect((screen.getByTestId('composer') as HTMLTextAreaElement).dataset.actionsDisabled).toBe('false')
  })

  it('fences same stored ids across profiles until the new runtime publishes', () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } }
    })

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/stored-1']}>
          <ChatView {...chatViewProps()} />
        </MemoryRouter>
      </QueryClientProvider>
    )

    expect((screen.getByTestId('composer') as HTMLTextAreaElement).dataset.actionsDisabled).toBe('false')

    act(() => $activeGatewayProfile.set('profile-b'))

    expect((screen.getByTestId('composer') as HTMLTextAreaElement).dataset.actionsDisabled).toBe('true')

    act(() => {
      $sessionStates.set({
        ...$sessionStates.get(),
        'runtime-2': {
          ...createClientSessionState('stored-1'),
          messages: [assistantMessage('assistant-b', 'Profile B is ready')]
        }
      })
      $activeSessionId.set('runtime-2')
    })

    expect((screen.getByTestId('composer') as HTMLTextAreaElement).dataset.actionsDisabled).toBe('false')
  })

  it('reaccepts the still-live original runtime after a committed owner A→B→A round trip', () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } }
    })

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/stored-1']}>
          <ChatView {...chatViewProps()} />
        </MemoryRouter>
      </QueryClientProvider>
    )

    expect((screen.getByTestId('composer') as HTMLTextAreaElement).dataset.actionsDisabled).toBe('false')

    act(() => $activeGatewayProfile.set('profile-b'))
    expect((screen.getByTestId('composer') as HTMLTextAreaElement).dataset.actionsDisabled).toBe('true')

    act(() => $activeGatewayProfile.set('profile-a'))
    expect((screen.getByTestId('composer') as HTMLTextAreaElement).dataset.actionsDisabled).toBe('false')
  })

  it('accepts a reused runtime id only after the owner generation turns over', () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } }
    })

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/stored-1']}>
          <ChatView {...chatViewProps()} />
        </MemoryRouter>
      </QueryClientProvider>
    )

    act(() => $activeGatewayProfile.set('profile-b'))
    expect((screen.getByTestId('composer') as HTMLTextAreaElement).dataset.actionsDisabled).toBe('true')

    // The route/profile handoff retires A's runtime generation before B can
    // publish. B is allowed to reuse the same backend runtime id.
    act(() => $activeSessionId.set(null))

    act(() => {
      $sessionStates.set({
        'runtime-1': {
          ...createClientSessionState('stored-1'),
          messages: [assistantMessage('assistant-b', 'Profile B reused the id')]
        }
      })
      $activeSessionId.set('runtime-1')
    })

    expect((screen.getByTestId('composer') as HTMLTextAreaElement).dataset.actionsDisabled).toBe('false')
  })
})
