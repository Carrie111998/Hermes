import { act, cleanup, render, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import type { ChatMessage } from '@/lib/chat-messages'
import { $dismissedPreviewPublications, $previewStatusBySession, dismissPreviewArtifact } from '@/store/preview-status'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $activeSessionId,
  $connection,
  $currentCwd,
  $messages,
  $selectedStoredSessionId,
  $sessions
} from '@/store/session'

import { previewArtifactPublications, PreviewArtifactPublisher } from './preview-artifact-publisher'

const TARGET = '/tmp/generated-preview.html'

const previewMessage = (
  id: string,
  overrides: { isError?: boolean; result?: { success: boolean }; toolCallId?: string } = {}
): ChatMessage => ({
  id: `message-${id}`,
  parts: [
    {
      args: { path: TARGET },
      result: { success: true },
      toolCallId: id,
      toolName: 'write_file',
      type: 'tool-call',
      ...overrides
    }
  ],
  role: 'assistant'
})

const createSessionView = (
  runtimeId = atom<string | null>(null),
  storedId = atom<string | null>('lineage-root'),
  messages = atom<ChatMessage[]>([]),
  cwd = atom('/tmp')
): SessionView => ({
  kind: 'primary',
  $awaitingResponse: atom(false),
  $busy: atom(false),
  $cwd: cwd,
  $fast: atom(false),
  $lastVisibleIsUser: atom(false),
  $messages: messages,
  $messagesEmpty: atom(false),
  $model: atom(''),
  $provider: atom(''),
  $reasoningEffort: atom(''),
  $runtimeId: runtimeId,
  $storedId: storedId
})

const renderPublisher = (view: SessionView) =>
  render(
    <SessionViewProvider value={view}>
      <PreviewArtifactPublisher />
    </SessionViewProvider>
  )

beforeEach(() => {
  localStorage.clear()
  $activeGatewayProfile.set('default')
  $connection.set({ baseUrl: 'http://127.0.0.1:4000', mode: 'local' } as never)
  $dismissedPreviewPublications.set([])
  $previewStatusBySession.set({})
  $sessions.set([{ id: 'lineage-root' }] as never)
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  $currentCwd.set('')
  $messages.set([])
})

afterEach(() => {
  cleanup()
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  $currentCwd.set('')
  $messages.set([])
})

describe('PreviewArtifactPublisher', () => {
  it('collects every completed publication in loaded history, not only rendered rows', () => {
    expect(
      previewArtifactPublications([
        previewMessage('old'),
        previewMessage('missing-id', { toolCallId: undefined }),
        previewMessage('pending', { result: undefined }),
        previewMessage('unsuccessful', { result: { success: false } }),
        previewMessage('failed', { isError: true }),
        previewMessage('new')
      ])
    ).toEqual([
      { publicationId: 'old', target: TARGET },
      { publicationId: 'new', target: TARGET }
    ])
  })

  it('discovers a completed replacement for a previously pending tool part', () => {
    expect(previewArtifactPublications([previewMessage('pending', { result: undefined })])).toEqual([])
    expect(previewArtifactPublications([previewMessage('pending')])).toEqual([
      { publicationId: 'pending', target: TARGET }
    ])
  })

  it('does not reparse an unchanged settled tool part on transcript-only updates', () => {
    const message = previewMessage('settled')
    const part = message.parts[0]

    expect(part?.type).toBe('tool-call')

    if (part?.type !== 'tool-call') {
      return
    }

    const result = part.result
    let resultReads = 0

    Object.defineProperty(part, 'result', {
      configurable: true,
      get: () => {
        resultReads += 1

        return result
      }
    })

    expect(previewArtifactPublications([message])).toHaveLength(1)
    const readsAfterFirstScan = resultReads

    expect(previewArtifactPublications([message])).toHaveLength(1)
    expect(resultReads).toBe(readsAfterFirstScan)
  })

  it('does not revisit stable history when the streaming tail is replaced', async () => {
    const historical = previewMessage('historical')
    const historicalParts = historical.parts
    let historicalReads = 0

    Object.defineProperty(historical, 'parts', {
      configurable: true,
      get: () => {
        historicalReads += 1

        return historicalParts
      }
    })

    const messages = atom<ChatMessage[]>([
      historical,
      { id: 'stream', parts: [{ text: 'a', type: 'text' }], role: 'assistant' }
    ])

    const view = createSessionView(atom('runtime-1'), atom('lineage-root'), messages)

    renderPublisher(view)
    await waitFor(() => expect($previewStatusBySession.get()['runtime-1']).toHaveLength(1))
    const readsAfterInitialScan = historicalReads

    act(() => {
      messages.set([
        historical,
        { id: 'stream', parts: [{ text: 'ab', type: 'text' }], role: 'assistant' }
      ])
    })

    expect(historicalReads).toBe(readsAfterInitialScan)
  })

  it('retries when the runtime becomes ready and dismisses every duplicate publication', async () => {
    const runtimeId = atom<string | null>(null)
    const messages = atom([previewMessage('old'), previewMessage('new')])
    const view = createSessionView(runtimeId, atom('lineage-root'), messages)

    const rendered = renderPublisher(view)

    expect($previewStatusBySession.get()['runtime-1']).toBeUndefined()

    act(() => runtimeId.set('runtime-1'))
    await waitFor(() => expect($previewStatusBySession.get()['runtime-1']).toHaveLength(1))

    const artifact = $previewStatusBySession.get()['runtime-1']![0]!
    act(() => dismissPreviewArtifact('runtime-1', artifact.id))
    expect($dismissedPreviewPublications.get()).toHaveLength(2)

    rendered.unmount()
    renderPublisher(view)
    expect($previewStatusBySession.get()['runtime-1']).toBeUndefined()

    act(() => messages.set([...messages.get(), previewMessage('fresh')]))
    await waitFor(() => expect($previewStatusBySession.get()['runtime-1']).toHaveLength(1))

    act(() => runtimeId.set('runtime-2'))
    await waitFor(() => expect($previewStatusBySession.get()['runtime-2']).toHaveLength(1))
    expect($previewStatusBySession.get()['runtime-1']).toBeUndefined()
  })

  it('waits for authoritative compression lineage before registering a new publication', async () => {
    const runtimeId = atom<string | null>('runtime-1')
    const storedId = atom<string | null>('lineage-root')
    const messages = atom([previewMessage('before-compression')])
    const view = createSessionView(runtimeId, storedId, messages)

    renderPublisher(view)
    await waitFor(() => expect($previewStatusBySession.get()['runtime-1']).toHaveLength(1))

    act(() => {
      storedId.set('compression-tip')
      messages.set([...messages.get(), previewMessage('after-compression')])
    })
    expect($previewStatusBySession.get()['runtime-1']).toBeUndefined()

    act(() => $sessions.set([{ id: 'compression-tip', _lineage_root_id: 'lineage-root' }] as never))
    await waitFor(() => expect($previewStatusBySession.get()['runtime-1']).toHaveLength(1))
  })

  it('clears publications while the session route is suppressed', async () => {
    const view = createSessionView(atom('runtime-1'), atom('lineage-root'), atom([previewMessage('visible')]))
    const rendered = renderPublisher(view)

    await waitFor(() => expect($previewStatusBySession.get()['runtime-1']).toHaveLength(1))

    rendered.rerender(
      <SessionViewProvider value={view}>
        <PreviewArtifactPublisher disabled />
      </SessionViewProvider>
    )

    expect($previewStatusBySession.get()['runtime-1']).toBeUndefined()
  })

  it('publishes only while its kept-alive pane is visible', async () => {
    const view = createSessionView(atom('runtime-1'), atom('lineage-root'), atom([previewMessage('visible')]))

    const rendered = render(
      <PaneVisibleContext value={false}>
        <SessionViewProvider value={view}>
          <PreviewArtifactPublisher />
        </SessionViewProvider>
      </PaneVisibleContext>
    )

    expect($previewStatusBySession.get()['runtime-1']).toBeUndefined()

    rendered.rerender(
      <PaneVisibleContext value>
        <SessionViewProvider value={view}>
          <PreviewArtifactPublisher />
        </SessionViewProvider>
      </PaneVisibleContext>
    )
    await waitFor(() => expect($previewStatusBySession.get()['runtime-1']).toHaveLength(1))

    rendered.rerender(
      <PaneVisibleContext value={false}>
        <SessionViewProvider value={view}>
          <PreviewArtifactPublisher />
        </SessionViewProvider>
      </PaneVisibleContext>
    )
    await waitFor(() => expect($previewStatusBySession.get()['runtime-1']).toBeUndefined())
  })

  it('publishes into the owning tile instead of the globally active primary session', async () => {
    $activeSessionId.set('primary-runtime')
    $currentCwd.set('/primary/work')

    const view = createSessionView(
      atom('tile-runtime'),
      atom('lineage-root'),
      atom([previewMessage('tile')]),
      atom('/tile/work')
    )

    renderPublisher(view)

    await waitFor(() => expect(Object.keys($previewStatusBySession.get())).toEqual(['tile-runtime']))
    expect($previewStatusBySession.get()['tile-runtime']?.[0]?.cwd).toBe('/tile/work')
  })

  it('still publishes through the default primary session view', async () => {
    $activeSessionId.set('primary-runtime')
    $selectedStoredSessionId.set('lineage-root')
    $currentCwd.set('/primary/work')
    $messages.set([previewMessage('primary')])

    render(<PreviewArtifactPublisher />)

    await waitFor(() => expect(Object.keys($previewStatusBySession.get())).toEqual(['primary-runtime']))
    expect($previewStatusBySession.get()['primary-runtime']?.[0]?.cwd).toBe('/primary/work')
  })

  it('does not reattribute a mounted runtime when profiles contain the same stored id', async () => {
    const runtimeId = atom<string | null>('runtime-default')
    const messages = atom([previewMessage('same-id')])
    const view = createSessionView(runtimeId, atom('same-tip'), messages)

    $sessions.set([{ id: 'same-tip', _lineage_root_id: 'default-root', profile: 'default' }] as never)
    renderPublisher(view)
    await waitFor(() => expect($previewStatusBySession.get()['runtime-default']).toHaveLength(1))

    act(() => {
      $activeGatewayProfile.set('work')
      $sessions.set([{ id: 'same-tip', _lineage_root_id: 'work-root', profile: 'work' }] as never)
    })

    expect($previewStatusBySession.get()['runtime-default']).toBeUndefined()

    act(() => runtimeId.set('runtime-work'))
    await waitFor(() => expect($previewStatusBySession.get()['runtime-work']).toHaveLength(1))
  })

  it('does not reattribute a mounted runtime when the active backend changes', async () => {
    const runtimeId = atom<string | null>('runtime-local')
    const view = createSessionView(runtimeId, atom('lineage-root'), atom([previewMessage('same-id')]))

    renderPublisher(view)
    await waitFor(() => expect($previewStatusBySession.get()['runtime-local']).toHaveLength(1))

    act(() => {
      $connection.set({ baseUrl: 'https://remote.example.test', mode: 'remote' } as never)
    })

    expect($previewStatusBySession.get()['runtime-local']).toBeUndefined()

    act(() => runtimeId.set('runtime-remote'))
    await waitFor(() => expect($previewStatusBySession.get()['runtime-remote']).toHaveLength(1))
  })

  it('reconciles authoritative empty history by clearing prior publications', async () => {
    const messages = atom([previewMessage('removed')])
    const view = createSessionView(atom('runtime-1'), atom('lineage-root'), messages)

    renderPublisher(view)
    await waitFor(() => expect($previewStatusBySession.get()['runtime-1']).toHaveLength(1))

    act(() => messages.set([]))

    expect($previewStatusBySession.get()['runtime-1']).toBeUndefined()
  })

  it('clears the concrete runtime on a null readiness gap and unmount', async () => {
    const runtimeId = atom<string | null>('runtime-1')
    const storedId = atom<string | null>('lineage-root')
    const messages = atom([previewMessage('gap')])
    const view = createSessionView(runtimeId, storedId, messages)

    const rendered = renderPublisher(view)

    await waitFor(() => expect($previewStatusBySession.get()['runtime-1']).toHaveLength(1))

    act(() => runtimeId.set(null))
    expect($previewStatusBySession.get()['runtime-1']).toBeUndefined()

    act(() => runtimeId.set('runtime-2'))
    await waitFor(() => expect($previewStatusBySession.get()['runtime-2']).toHaveLength(1))

    rendered.unmount()
    expect($previewStatusBySession.get()['runtime-2']).toBeUndefined()
  })
})
