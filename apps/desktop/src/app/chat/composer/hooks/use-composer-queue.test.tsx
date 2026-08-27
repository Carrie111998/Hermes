import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $parkedQueueSessions,
  $queuedPromptsBySession,
  enqueueQueuedPrompt,
  getQueuedPrompts,
  isQueueParked,
  parkQueuedPrompts
} from '@/store/composer-queue'
import { _resetComposerQueueDrainsForTests } from '@/store/composer-queue-drain'
import {
  _resetComposerStorageMigrationsForTests,
  migrateComposerStorageScope
} from '@/store/composer-storage-migration'
import { decodeComposerStorageScopeKey, encodeComposerStorageScopeKey } from '@/store/composer-storage-scope'

import type { QueueEditState } from '../composer-utils'
import type { ChatBarProps } from '../types'

import { useComposerQueue } from './use-composer-queue'

// The park ↔ drain contract at the hook level. The store tests pin the pure
// pieces (shouldAutoDrain, park bookkeeping); these pin the wiring — the
// auto-drain effect honoring the park, and send-now-while-busy lifting it so
// the settle drain still flows (the regression that sank the old blanket
// interrupt latch).

const OWNER = { connectionId: 'connection-a', profile: 'profile-a' }
const storageKey = (storedSessionId: string | null) => encodeComposerStorageScopeKey(OWNER, storedSessionId)
const SESSION_KEY = storageKey('stored-session-queue-hook')

function renderQueueHook(
  overrides: {
    actionsDisabled?: boolean
    busy?: boolean
    onCancel?: () => void
    onSteer?: ChatBarProps['onSteer']
    onSubmit?: ChatBarProps['onSubmit']
    queueSessionKey?: string | null
    runtimeDerived?: boolean
    sessionKey?: string
    submitScopeKey?: string | null
  } = {}
) {
  const onSubmit = vi.fn<ChatBarProps['onSubmit']>(overrides.onSubmit ?? (async () => true))
  const onCancel = overrides.onCancel ?? vi.fn()
  const onSteer = overrides.onSteer
  const queueEditRef: { current: QueueEditState | null } = { current: null }
  const draftRef = { current: '' }
  const loadIntoComposer = vi.fn()

  const initialProps: {
    actionsDisabled?: boolean
    busy: boolean
    queueSessionKey?: string | null
    sessionKey?: string
  } = {
    actionsDisabled: overrides.actionsDisabled,
    busy: overrides.busy ?? false,
    queueSessionKey: overrides.queueSessionKey,
    sessionKey: overrides.sessionKey
  }

  const hook = renderHook(
    ({
      actionsDisabled,
      busy,
      queueSessionKey,
      sessionKey
    }: {
      actionsDisabled?: boolean
      busy: boolean
      queueSessionKey?: string | null
      sessionKey?: string
    }) => {
      const activeSessionKey = sessionKey ?? overrides.sessionKey ?? SESSION_KEY
      const rawSessionKey = decodeComposerStorageScopeKey(activeSessionKey)?.storedSessionId ?? activeSessionKey
      const durableQueueSessionKey = queueSessionKey === undefined ? rawSessionKey : queueSessionKey

      return useComposerQueue({
        actionsDisabled: actionsDisabled ?? overrides.actionsDisabled ?? false,
        activeQueueSessionKey: activeSessionKey,
        attachments: [],
        busy,
        clearDraft: () => undefined,
        draftRef,
        focusInput: () => undefined,
        loadIntoComposer,
        onCancel,
        onSteer,
        onSubmit,
        queueEditRef,
        queueSessionKey: overrides.runtimeDerived ? undefined : durableQueueSessionKey,
        sessionId: `rt-${activeSessionKey}`,
        submitScopeKey: overrides.submitScopeKey ?? durableQueueSessionKey
      })
    },
    { initialProps }
  )

  return { draftRef, hook, loadIntoComposer, onCancel, onSubmit }
}

describe('useComposerQueue park integration', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
    _resetComposerQueueDrainsForTests()
    _resetComposerStorageMigrationsForTests()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
    _resetComposerQueueDrainsForTests()
    _resetComposerStorageMigrationsForTests()
  })

  it('auto-drains an unparked queue once idle', async () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'flows' })

    const { onSubmit } = renderQueueHook()

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(0)
  })

  it('keeps a profile-qualified queue local while submitting the raw stored id', async () => {
    const qualifiedKey = storageKey('stored-1')

    enqueueQueuedPrompt(qualifiedKey, { attachments: [], text: 'profile A queue' })

    const { onSubmit } = renderQueueHook({
      queueSessionKey: 'stored-1',
      sessionKey: qualifiedKey,
      submitScopeKey: 'stored-1'
    })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    expect(onSubmit.mock.calls[0]?.[1]).toMatchObject({
      composerStorageScope: qualifiedKey,
      storedSessionId: 'stored-1'
    })
    expect(getQueuedPrompts(qualifiedKey)).toHaveLength(0)
  })

  it('holds every queued action while route and active session disagree', async () => {
    const entry = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'belongs to the new route' })
    const onSteer = vi.fn(async () => true)
    const { hook, onCancel, onSubmit } = renderQueueHook({ actionsDisabled: true, busy: true, onSteer })

    act(() => {
      expect(hook.result.current.sendQueuedNow(entry!.id)).toBe(false)
    })

    await act(async () => {
      expect(await hook.result.current.steerQueuedNow(entry!.id)).toBe(false)
      expect(await hook.result.current.drainNextQueued()).toBe(false)
      hook.rerender({ busy: false })
      await Promise.resolve()
    })

    expect(onCancel).not.toHaveBeenCalled()
    expect(onSteer).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(1)
  })

  it('blocks queue edit, save, cancel, and stepping while identities disagree', () => {
    const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'first queued draft' })!
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'second queued draft' })
    const { draftRef, hook, loadIntoComposer } = renderQueueHook({ busy: true })

    act(() => hook.result.current.beginQueuedEdit(first))
    expect(hook.result.current.queueEdit?.entryId).toBe(first.id)

    loadIntoComposer.mockClear()
    draftRef.current = 'draft typed during transition'
    hook.rerender({ actionsDisabled: true, busy: true })

    act(() => {
      expect(hook.result.current.stepQueuedEdit(1)).toBe(false)
      expect(hook.result.current.exitQueuedEdit('save')).toBe(false)
      expect(hook.result.current.exitQueuedEdit('cancel')).toBe(false)
      hook.result.current.beginQueuedEdit(first)
    })

    expect(getQueuedPrompts(SESSION_KEY)[0]?.text).toBe('first queued draft')
    expect(hook.result.current.queueEdit?.entryId).toBe(first.id)
    expect(loadIntoComposer).not.toHaveBeenCalled()
  })

  it('drains B after an in-flight A drain settles without requiring another B event', async () => {
    const sessionA = storageKey('session-a')
    const sessionB = storageKey('session-b')
    let settleA: ((accepted: boolean) => void) | undefined

    const pendingA = new Promise<boolean>(resolve => {
      settleA = resolve
    })

    const onSubmit = vi.fn<ChatBarProps['onSubmit']>((text: string) =>
      text === 'queued in A' ? pendingA : Promise.resolve(true)
    )

    enqueueQueuedPrompt(sessionA, { attachments: [], text: 'queued in A' })
    enqueueQueuedPrompt(sessionB, { attachments: [], text: 'queued in B' })
    const { hook } = renderQueueHook({ onSubmit, sessionKey: sessionA })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('queued in A', expect.anything()))

    hook.rerender({ busy: false, sessionKey: sessionB })

    await act(async () => settleA?.(true))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('queued in B', expect.anything()))
    expect(getQueuedPrompts(sessionB)).toHaveLength(0)
  })

  it('does not submit an in-flight entry twice when its runtime-derived queue key migrates', async () => {
    const sessionA = storageKey('runtime-a')
    const sessionB = storageKey('runtime-b')
    let settle: ((accepted: boolean) => void) | undefined

    const pending = new Promise<boolean>(resolve => {
      settle = resolve
    })

    const onSubmit = vi.fn<ChatBarProps['onSubmit']>(() => pending)

    enqueueQueuedPrompt(sessionA, { attachments: [], text: 'migrating entry' })
    const { hook } = renderQueueHook({ onSubmit, runtimeDerived: true, sessionKey: sessionA })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    hook.rerender({ busy: false, sessionKey: sessionB })

    await waitFor(() => expect(getQueuedPrompts(sessionB)).toHaveLength(1))
    expect(onSubmit).toHaveBeenCalledTimes(1)

    await act(async () => settle?.(true))

    await waitFor(() => expect(getQueuedPrompts(sessionB)).toHaveLength(0))
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })

  it('hands an in-flight qualified tip drain lock and removal target to the lineage root', async () => {
    const tipKey = storageKey('tip-a')
    const rootKey = storageKey('root-a')
    let settle: ((accepted: boolean) => void) | undefined

    const pending = new Promise<boolean>(resolve => {
      settle = resolve
    })

    const onSubmit = vi.fn<ChatBarProps['onSubmit']>(() => pending)

    enqueueQueuedPrompt(tipKey, { attachments: [], text: 'lineage handoff' })

    const { hook } = renderQueueHook({
      onSubmit,
      queueSessionKey: 'tip-a',
      sessionKey: tipKey,
      submitScopeKey: 'tip-a'
    })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))

    act(() => {
      migrateComposerStorageScope(tipKey, rootKey)
      hook.rerender({ busy: false, queueSessionKey: 'root-a', sessionKey: rootKey })
    })

    expect(onSubmit).toHaveBeenCalledTimes(1)

    await act(async () => settle?.(true))

    await waitFor(() => expect(getQueuedPrompts(rootKey)).toHaveLength(0))
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })

  it('retries A automatically after a stale A drain rejects across A to B to A', async () => {
    const sessionA = storageKey('session-a')
    const sessionB = storageKey('session-b')
    let rejectFirst: ((accepted: boolean) => void) | undefined

    const firstPending = new Promise<boolean>(resolve => {
      rejectFirst = resolve
    })

    const onSubmit = vi
      .fn<ChatBarProps['onSubmit']>()
      .mockImplementationOnce(() => firstPending)
      .mockResolvedValue(true)

    enqueueQueuedPrompt(sessionA, { attachments: [], text: 'retry after stale rejection' })
    const { hook } = renderQueueHook({ onSubmit, sessionKey: sessionA })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    hook.rerender({ busy: false, queueSessionKey: 'session-b', sessionKey: sessionB })
    hook.rerender({ busy: false, queueSessionKey: 'session-a', sessionKey: sessionA })

    await act(async () => rejectFirst?.(false))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2))
    expect(getQueuedPrompts(sessionA)).toHaveLength(0)
  })

  it('holds a parked queue at the idle settle (the Stop edge)', async () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'halted' })
    parkQueuedPrompts(SESSION_KEY)

    const { hook, onSubmit } = renderQueueHook({ busy: true })

    // The Stop settle: busy flips false with the park in place.
    hook.rerender({ busy: false })

    await act(async () => {
      await Promise.resolve()
    })

    expect(onSubmit).not.toHaveBeenCalled()
    expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(1)
  })

  it('drainNextQueued sends a parked entry and lifts the park (manual resume)', async () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'resumed' })
    parkQueuedPrompts(SESSION_KEY)

    const { hook, onSubmit } = renderQueueHook()

    await act(async () => {
      await hook.result.current.drainNextQueued()
    })

    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(isQueueParked(SESSION_KEY)).toBe(false)
  })

  it('sendQueuedNow while busy unparks so the settle drain flows (no stale latch)', async () => {
    const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'first' })
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'send me now' })
    parkQueuedPrompts(SESSION_KEY)

    const { hook, onCancel, onSubmit } = renderQueueHook({ busy: true })
    const target = getQueuedPrompts(SESSION_KEY).find(e => e.id !== first!.id)!

    act(() => {
      hook.result.current.sendQueuedNow(target.id)
    })

    // The interrupt fired and the park lifted — this interrupt exists to reach
    // the queue, not to halt it.
    expect(onCancel).toHaveBeenCalledTimes(1)
    expect(isQueueParked(SESSION_KEY)).toBe(false)

    // Turn settles → the promoted entry drains.
    hook.rerender({ busy: false })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0]?.[0]).toBe('send me now')
  })

  it('steerQueuedNow delivers via onSteer without cancelling and removes the entry', async () => {
    const entry = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'steer me' })
    const onSteer = vi.fn(async () => true)
    const { hook, onCancel, onSubmit } = renderQueueHook({ busy: true, onSteer })

    await act(async () => {
      expect(await hook.result.current.steerQueuedNow(entry!.id)).toBe(true)
    })

    expect(onSteer).toHaveBeenCalledWith('steer me')
    // A redirect rides the live turn: no interrupt, no submit.
    expect(onCancel).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(0)
  })

  it('serializes a pending steer and removes it from the migrated destination', async () => {
    const destination = storageKey('stored-after-steer')
    const entry = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'one redirect only' })
    let settle: ((accepted: boolean) => void) | undefined

    const pending = new Promise<boolean>(resolve => {
      settle = resolve
    })

    const onSteer = vi.fn(() => pending)
    const { hook } = renderQueueHook({ busy: true, onSteer })
    let first: Promise<boolean>
    let second: Promise<boolean>

    act(() => {
      first = hook.result.current.steerQueuedNow(entry!.id)
    })

    await waitFor(() => expect(onSteer).toHaveBeenCalledOnce())

    act(() => {
      second = hook.result.current.steerQueuedNow(entry!.id)
    })

    expect(onSteer).toHaveBeenCalledOnce()

    act(() => {
      migrateComposerStorageScope(SESSION_KEY, destination)
      hook.rerender({ busy: true, queueSessionKey: 'stored-after-steer', sessionKey: destination })
    })

    await act(async () => settle?.(true))

    await expect(first!).resolves.toBe(true)
    await expect(second!).resolves.toBe(false)
    expect(onSteer).toHaveBeenCalledOnce()
    expect(getQueuedPrompts(destination)).toHaveLength(0)
  })

  it('does not clear a newer Stop park when a pending steer succeeds', async () => {
    const entry = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'pending redirect' })
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'must stay parked' })
    let settle: ((accepted: boolean) => void) | undefined

    const pending = new Promise<boolean>(resolve => {
      settle = resolve
    })

    const onSteer = vi.fn(() => pending)
    const { hook } = renderQueueHook({ busy: true, onSteer })
    let steering: Promise<boolean>

    act(() => {
      steering = hook.result.current.steerQueuedNow(entry!.id)
    })

    await waitFor(() => expect(onSteer).toHaveBeenCalledOnce())

    act(() => {
      parkQueuedPrompts(SESSION_KEY)
    })

    await act(async () => settle?.(true))

    await expect(steering!).resolves.toBe(true)
    expect(isQueueParked(SESSION_KEY)).toBe(true)
    expect(getQueuedPrompts(SESSION_KEY).map(item => item.text)).toEqual(['must stay parked'])
  })

  it('a rejected steer leaves the entry queued so the settle drain still sends it', async () => {
    const entry = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'kept on reject' })
    const onSteer = vi.fn(async () => false)
    const { hook, onSubmit } = renderQueueHook({ busy: true, onSteer })

    await act(async () => {
      expect(await hook.result.current.steerQueuedNow(entry!.id)).toBe(false)
    })

    expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(1)

    // Turn settles → the surviving entry drains normally.
    hook.rerender({ busy: false })
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0]?.[0]).toBe('kept on reject')
  })

  it('steerQueuedNow refuses unsteerable entries (slash commands execute, never steer)', async () => {
    const slash = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: '/compress' })
    const onSteer = vi.fn(async () => true)

    // Busy, but a slash command never steers. (Idle needs no case of its own:
    // an idle session auto-drains its queue, so there is never an entry left
    // to steer — asserting that here would just re-test auto-drain.)
    const busy = renderQueueHook({ busy: true, onSteer })

    await act(async () => {
      expect(await busy.hook.result.current.steerQueuedNow(slash!.id)).toBe(false)
    })

    expect(onSteer).not.toHaveBeenCalled()
    expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(1)
  })

  it('a delivered steer lifts the park so the rest of the queue flows', async () => {
    const steerable = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'redirect' })
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'follows after' })
    parkQueuedPrompts(SESSION_KEY)

    const onSteer = vi.fn(async () => true)
    const { hook } = renderQueueHook({ busy: true, onSteer })

    await act(async () => {
      expect(await hook.result.current.steerQueuedNow(steerable!.id)).toBe(true)
    })

    expect(isQueueParked(SESSION_KEY)).toBe(false)
    expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(1)
  })
})
