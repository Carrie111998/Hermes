import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $parkedQueueSessions,
  $queuedPromptsBySession,
  enqueueQueuedPrompt,
  getQueuedPrompts,
  isQueueParked,
  MAX_AUTO_DRAIN_ATTEMPTS,
  parkQueuedPrompts,
  queueStuckNoticeId,
  resetQueueDrainState
} from '@/store/composer-queue'
import { $notifications } from '@/store/notifications'

import type { QueueEditState } from '../composer-utils'
import type { ChatBarProps } from '../types'

import { useComposerQueue } from './use-composer-queue'

// The park ↔ drain contract at the hook level. The store tests pin the pure
// pieces (shouldAutoDrain, park bookkeeping); these pin the wiring — the
// auto-drain effect honoring the park, and send-now-while-busy lifting it so
// the settle drain still flows (the regression that sank the old blanket
// interrupt latch).

const SESSION_KEY = 'stored-session-queue-hook'

function renderQueueHook(overrides: { busy?: boolean; onCancel?: () => void; onSteer?: ChatBarProps['onSteer'] } = {}) {
  const onSubmit = vi.fn<ChatBarProps['onSubmit']>(async () => true)
  const onCancel = overrides.onCancel ?? vi.fn()
  const onSteer = overrides.onSteer
  const queueEditRef: { current: QueueEditState | null } = { current: null }

  const hook = renderHook(
    ({ busy }: { busy: boolean }) =>
      useComposerQueue({
        activeQueueSessionKey: SESSION_KEY,
        attachments: [],
        busy,
        clearDraft: () => undefined,
        draftRef: { current: '' },
        focusInput: () => undefined,
        loadIntoComposer: () => undefined,
        onCancel,
        onSteer,
        onSubmit,
        queueEditRef,
        queueSessionKey: SESSION_KEY,
        sessionId: 'rt-session-queue-hook'
      }),
    { initialProps: { busy: overrides.busy ?? false } }
  )

  return { hook, onCancel, onSubmit }
}

describe('useComposerQueue park integration', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
    resetQueueDrainState()
    $notifications.set([])
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
    resetQueueDrainState()
    $notifications.set([])
  })

  it('auto-drains an unparked queue once idle', async () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'flows' })

    const { onSubmit } = renderQueueHook()

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(0)
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

describe('useComposerQueue give-up', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
    resetQueueDrainState()
    $notifications.set([])
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    $queuedPromptsBySession.set({})
    $parkedQueueSessions.set({})
    resetQueueDrainState()
    $notifications.set([])
  })

  const mountDrainer = (onSubmit: ChatBarProps['onSubmit']) => {
    const queueEditRef: { current: QueueEditState | null } = { current: null }

    return renderHook(
      ({ busy }: { busy: boolean }) =>
        useComposerQueue({
          activeQueueSessionKey: SESSION_KEY,
          attachments: [],
          busy,
          clearDraft: () => undefined,
          draftRef: { current: '' },
          focusInput: () => undefined,
          loadIntoComposer: () => undefined,
          onCancel: vi.fn(),
          onSteer: undefined,
          onSubmit,
          queueEditRef,
          queueSessionKey: SESSION_KEY,
          sessionId: 'rt-session-queue-hook'
        }),
      { initialProps: { busy: false } }
    )
  }

  /** The composer drains on the idle edge, so each further attempt needs one —
   *  a turn starting and settling is what a real session does between tries. */
  const bounceBusy = async (hook: ReturnType<typeof mountDrainer>, times: number) => {
    for (let i = 0; i < times; i += 1) {
      await act(async () => {
        hook.rerender({ busy: true })
        await Promise.resolve()
      })
      await act(async () => {
        hook.rerender({ busy: false })
        await Promise.resolve()
      })
    }
  }

  it('offers a way to retry, because the alarm names one', async () => {
    const onSubmit = vi.fn<ChatBarProps['onSubmit']>(async () => false)

    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'refused four times' })

    const hook = mountDrainer(onSubmit)
    await bounceBusy(hook, MAX_AUTO_DRAIN_ATTEMPTS)

    const toast = $notifications.get().find(n => n.id === queueStuckNoticeId(SESSION_KEY))

    expect(toast).toBeTruthy()
    // The toast body is not a button — only the action is. Without one, the
    // message told the user to send it again and gave them nothing to press.
    expect(toast?.action).toBeTruthy()

    const attempts = onSubmit.mock.calls.length

    await act(async () => {
      toast?.action?.onClick()
      await Promise.resolve()
    })

    expect(onSubmit.mock.calls.length).toBeGreaterThan(attempts)
  })

  it('takes the alarm down when the entry finally sends', async () => {
    const onSubmit = vi.fn<ChatBarProps['onSubmit']>(async () => false)

    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'sends on the retry' })

    const hook = mountDrainer(onSubmit)
    await bounceBusy(hook, MAX_AUTO_DRAIN_ATTEMPTS)

    const toast = $notifications.get().find(n => n.id === queueStuckNoticeId(SESSION_KEY))
    expect(toast).toBeTruthy()

    onSubmit.mockResolvedValue(true)

    await act(async () => {
      toast?.action?.onClick()
      await Promise.resolve()
    })

    await waitFor(() => expect(getQueuedPrompts(SESSION_KEY)).toHaveLength(0))
    expect($notifications.get().some(n => n.id === queueStuckNoticeId(SESSION_KEY))).toBe(false)
  })

  it('does not keep alarming after a remount, which used to reset the budget', async () => {
    const onSubmit = vi.fn<ChatBarProps['onSubmit']>(async () => false)

    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'unsendable' })

    const first = mountDrainer(onSubmit)
    await bounceBusy(first, MAX_AUTO_DRAIN_ATTEMPTS)
    expect($notifications.get().some(n => n.id === queueStuckNoticeId(SESSION_KEY))).toBe(true)

    const spent = onSubmit.mock.calls.length
    first.unmount()
    $notifications.set([])

    // A session switch or a reconnect. The counter used to live on this
    // component, so this bought four more attempts and one more toast — forever,
    // since the queue itself survives in localStorage.
    const second = mountDrainer(onSubmit)
    await bounceBusy(second, MAX_AUTO_DRAIN_ATTEMPTS)

    expect(onSubmit.mock.calls.length).toBe(spent)
    expect($notifications.get()).toHaveLength(0)
  })
})
