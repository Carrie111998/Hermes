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

import type { QueueEditState } from '../composer-utils'
import type { ChatBarProps } from '../types'

import { useComposerQueue } from './use-composer-queue'

// The park ↔ drain contract at the hook level. The store tests pin the pure
// pieces (shouldAutoDrain, park bookkeeping); these pin the wiring — the
// auto-drain effect honoring the park, and send-now-while-busy lifting it so
// the settle drain still flows (the regression that sank the old blanket
// interrupt latch).

const SESSION_KEY = 'stored-session-queue-hook'

function renderQueueHook(
  overrides: {
    actionsDisabled?: boolean
    busy?: boolean
    onCancel?: () => void
    onSteer?: ChatBarProps['onSteer']
    onSubmit?: ChatBarProps['onSubmit']
    sessionKey?: string
  } = {}
) {
  const onSubmit = vi.fn<ChatBarProps['onSubmit']>(overrides.onSubmit ?? (async () => true))
  const onCancel = overrides.onCancel ?? vi.fn()
  const onSteer = overrides.onSteer
  const queueEditRef: { current: QueueEditState | null } = { current: null }
  const draftRef = { current: '' }
  const loadIntoComposer = vi.fn()

  const initialProps: { actionsDisabled?: boolean; busy: boolean; sessionKey?: string } = {
    actionsDisabled: overrides.actionsDisabled,
    busy: overrides.busy ?? false,
    sessionKey: overrides.sessionKey
  }

  const hook = renderHook(
    ({ actionsDisabled, busy, sessionKey }: { actionsDisabled?: boolean; busy: boolean; sessionKey?: string }) => {
      const activeSessionKey = sessionKey ?? overrides.sessionKey ?? SESSION_KEY

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
        queueSessionKey: activeSessionKey,
        sessionId: `rt-${activeSessionKey}`
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
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
  })

  it('auto-drains an unparked queue once idle', async () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'flows' })

    const { onSubmit } = renderQueueHook()

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(0)
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
    const sessionA = 'session-a'
    const sessionB = 'session-b'
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
