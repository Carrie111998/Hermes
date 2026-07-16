import { act, renderHook, waitFor } from '@testing-library/react'
import type { RefObject } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { enqueueQueuedPrompt, getQueuedPrompts } from '@/store/composer-queue'
import { installFakeLocks, resetQueueStorage } from '@/store/composer-queue-test-utils'
import { notify } from '@/store/notifications'

import type { QueueEditState } from '../composer-utils'

import { useComposerQueue } from './use-composer-queue'

vi.mock('@/lib/haptics', () => ({ triggerHaptic: () => {} }))
vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      composer: {
        queueBusyElsewhereBody: 'busy elsewhere body',
        queueBusyElsewhereTitle: 'busy elsewhere title',
        queueStuckBody: 'stuck body',
        queueStuckTitle: 'stuck title'
      }
    }
  })
}))
vi.mock('@/store/notifications', () => ({ notify: vi.fn() }))
vi.mock('@/store/composer', () => ({ clearComposerAttachments: () => {} }))
vi.mock('@/store/composer-input-history', () => ({ resetBrowseState: () => {} }))
vi.mock('../composer-utils', () => ({
  cloneAttachments: (attachments: unknown[]) => attachments.map(a => ({ ...(a as object) }))
}))

const SESSION_KEY = 'session-drain'

type OnSubmit = (value: string, options?: { fromQueue?: boolean }) => Promise<boolean> | boolean

// One hook instance = one desktop window's composer. Instances share
// localStorage and the (fake) lock manager exactly like real windows do; they
// additionally share the module-level atom, which only makes the race harsher —
// both "windows" see a new head entry instantly, with zero storage-event lag.
function renderWindow(onSubmit: OnSubmit, { busy = false }: { busy?: boolean } = {}) {
  const draftRef: RefObject<string> = { current: '' }
  const queueEditRef: RefObject<QueueEditState | null> = { current: null }

  return renderHook(() =>
    useComposerQueue({
      activeQueueSessionKey: SESSION_KEY,
      attachments: [],
      busy,
      clearDraft: () => {},
      draftRef,
      focusInput: () => {},
      loadIntoComposer: () => {},
      onCancel: () => {},
      onSubmit,
      queueEditRef,
      queueSessionKey: SESSION_KEY,
      sessionId: SESSION_KEY
    })
  )
}

describe('useComposerQueue cross-window drain (#57516 review)', () => {
  beforeEach(() => {
    vi.mocked(notify).mockClear()
    resetQueueStorage()
  })

  it('auto-drains an entry exactly once across two concurrently idle windows', async () => {
    const restore = installFakeLocks()

    try {
      enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'send me once' })

      // Window A wins the claim and is stuck mid-submit (in-flight request).
      let settleA!: (accepted: boolean) => void
      const onSubmitA = vi.fn(() => new Promise<boolean>(resolve => (settleA = resolve)))
      const windowA = renderWindow(onSubmitA)
      await act(async () => {})

      expect(onSubmitA).toHaveBeenCalledTimes(1)

      // Window B goes idle while A's submit is still in flight: same head entry
      // visible, but the claim is held, so B must not submit it.
      const onSubmitB = vi.fn(() => Promise.resolve(true))
      const windowB = renderWindow(onSubmitB)
      await act(async () => {})

      expect(onSubmitB).not.toHaveBeenCalled()

      await act(async () => {
        settleA(true)
      })

      expect(onSubmitA).toHaveBeenCalledTimes(1)
      expect(onSubmitB).not.toHaveBeenCalled()
      expect(getQueuedPrompts(SESSION_KEY)).toEqual([])

      windowA.unmount()
      windowB.unmount()
    } finally {
      restore()
    }
  })

  it('does not resubmit an entry another window already drained (stale local state)', async () => {
    const onSubmit = vi.fn(() => Promise.resolve(true))
    const { result, unmount } = renderWindow(onSubmit, { busy: true }) // busy: no auto-drain
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'drained elsewhere' })

    // Another window drains the entry; its storage write is visible but the
    // `storage` event syncing our atom has not fired yet.
    window.localStorage.removeItem('hermes.desktop.composerQueue.v1')

    await act(async () => {
      await expect(result.current.drainNextQueued()).resolves.toBe(false)
    })

    expect(onSubmit).not.toHaveBeenCalled()
    unmount()
  })

  it('auto-drains normally in a single idle window (sanity)', async () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'plain send' })

    const onSubmit = vi.fn(() => Promise.resolve(true))
    const { unmount } = renderWindow(onSubmit)
    await act(async () => {})

    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit).toHaveBeenCalledWith('plain send', { attachments: [], fromQueue: true })
    expect(getQueuedPrompts(SESSION_KEY)).toEqual([])
    unmount()
  })
})

describe('useComposerQueue drain liveness (#57516 follow-up)', () => {
  beforeEach(() => {
    vi.mocked(notify).mockClear()
    resetQueueStorage()
  })

  it('a losing window retries after the winner’s claim releases without a send (winner rejected/crashed)', async () => {
    const restore = installFakeLocks()

    try {
      enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'strand me not' })

      // Window A wins the claim, then its submit is REJECTED: it writes no
      // storage, so no storage event will ever reach window B.
      let settleA!: (accepted: boolean) => void
      const onSubmitA = vi.fn(() => new Promise<boolean>(resolve => (settleA = resolve)))
      const windowA = renderWindow(onSubmitA)
      await act(async () => {})

      expect(onSubmitA).toHaveBeenCalledTimes(1)

      const onSubmitB = vi.fn(() => Promise.resolve(true))
      const windowB = renderWindow(onSubmitB)
      await act(async () => {})

      expect(onSubmitB).not.toHaveBeenCalled()

      // A's submit is rejected → A releases the claim with no storage write.
      // B's claim-release waiter must wake it up to drain the entry itself.
      await act(async () => {
        settleA(false)
      })

      await waitFor(() => expect(onSubmitB).toHaveBeenCalledTimes(1))
      await act(async () => {})

      expect(getQueuedPrompts(SESSION_KEY)).toEqual([])

      windowA.unmount()
      windowB.unmount()
    } finally {
      restore()
    }
  })

  it('a manual send-now waits for another window’s in-flight drain instead of silently dropping the tap', async () => {
    const restore = installFakeLocks()

    try {
      const first = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'auto head' })
      const second = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'tapped entry' })

      expect(first).not.toBeNull()
      expect(second).not.toBeNull()

      // Window A auto-drains the head and is mid-submit, holding the claim.
      let settleA!: (accepted: boolean) => void
      const onSubmitA = vi.fn(() => new Promise<boolean>(resolve => (settleA = resolve)))
      const windowA = renderWindow(onSubmitA)
      await act(async () => {})

      expect(onSubmitA).toHaveBeenCalledTimes(1)

      // The user taps "send now" on the second entry in window B: the tap must
      // wait for A's claim, not vanish.
      const onSubmitB = vi.fn(() => Promise.resolve(true))
      const windowB = renderWindow(onSubmitB, { busy: true }) // busy: isolate the manual path

      let sendResolved: null | boolean = null
      let sendNow!: Promise<boolean>

      // windowB renders busy, so sendQueuedNow's busy branch would promote
      // instead of sending. Re-render idle w/o triggering auto-drain first:
      // manual path only.
      windowB.unmount()
      const windowB2 = renderWindow(onSubmitB, { busy: false })
      await act(async () => {})

      // Auto-drain in B2 lost the claim (contended) — expected. Now the tap:
      await act(async () => {
        sendNow = windowB2.result.current.sendQueuedNow(second!.id)
        void sendNow.then(sent => (sendResolved = sent))
      })

      expect(sendResolved).toBeNull()
      expect(onSubmitB).not.toHaveBeenCalled()

      // A's drain completes; B's waiting tap acquires the claim and sends the
      // exact entry the user tapped.
      await act(async () => {
        settleA(true)
      })

      await waitFor(() => expect(onSubmitB).toHaveBeenCalledTimes(1))
      expect(onSubmitB).toHaveBeenCalledWith('tapped entry', { attachments: [], fromQueue: true })
      await expect(sendNow).resolves.toBe(true)

      windowA.unmount()
      windowB2.unmount()
    } finally {
      restore()
    }
  })

  it('a tapped entry that no longer exists does not interrupt a live turn', async () => {
    const onCancel = vi.fn()
    const onSubmit = vi.fn(() => Promise.resolve(true))
    const draftRef: RefObject<string> = { current: '' }
    const queueEditRef: RefObject<QueueEditState | null> = { current: null }

    const { result, unmount } = renderHook(() =>
      useComposerQueue({
        activeQueueSessionKey: SESSION_KEY,
        attachments: [],
        busy: true,
        clearDraft: () => {},
        draftRef,
        focusInput: () => {},
        loadIntoComposer: () => {},
        onCancel,
        onSubmit,
        queueEditRef,
        queueSessionKey: SESSION_KEY,
        sessionId: SESSION_KEY
      })
    )

    await act(async () => {
      await expect(result.current.sendQueuedNow('queued-phantom-id')).resolves.toBe(false)
    })

    expect(onCancel).not.toHaveBeenCalled()
    unmount()
  })

  it('retries a rejected auto-drain with backoff until it sends (bounded)', async () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'flaky send' })

    // First two submits are rejected, the third succeeds — well inside
    // MAX_AUTO_DRAIN_ATTEMPTS. Without the retry timer nothing would ever
    // re-run auto-drain: the effect deps do not change on a rejected send.
    let attempts = 0
    const onSubmit = vi.fn(() => Promise.resolve(++attempts >= 3))

    const { unmount } = renderWindow(onSubmit)
    await act(async () => {})

    expect(onSubmit).toHaveBeenCalledTimes(1)

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(3), { timeout: 8_000 })
    await waitFor(() => expect(getQueuedPrompts(SESSION_KEY)).toEqual([]))

    expect(vi.mocked(notify)).not.toHaveBeenCalled()
    unmount()
  }, 15_000)
})
