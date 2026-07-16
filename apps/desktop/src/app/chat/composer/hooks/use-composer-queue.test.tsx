import { act, renderHook, waitFor } from '@testing-library/react'
import type { RefObject } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  enqueueQueuedPrompt,
  getQueuedPrompts,
  markQueuedPromptSent,
  QUEUE_STORAGE_KEY,
  QUEUE_TOMBSTONES_STORAGE_KEY,
  withSessionDrainClaim
} from '@/store/composer-queue'
import { installFakeLocks, persistedQueueTexts, resetQueueStorage } from '@/store/composer-queue-test-utils'
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
// Real store, tiny retry backoff: the liveness tests drive real retries and
// must not spend seconds of wall clock per attempt.
vi.mock('@/store/composer-queue', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  AUTO_DRAIN_RETRY_BASE_MS: 20
}))

const SESSION_KEY = 'session-drain'

type OnSubmit = (value: string, options?: { fromQueue?: boolean }) => Promise<boolean> | boolean

interface WindowProps {
  busy: boolean
  sessionKey: string
}

// One hook instance = one desktop window's composer. Instances share
// localStorage and the (fake) lock manager exactly like real windows do; they
// additionally share the module-level atom, which only makes the race harsher —
// both "windows" see a new head entry instantly, with zero storage-event lag.
function renderWindow(onSubmit: OnSubmit, { busy = false, onCancel = () => {} } = {}) {
  const draftRef: RefObject<string> = { current: '' }
  const queueEditRef: RefObject<QueueEditState | null> = { current: null }

  const rendered = renderHook(
    (props: WindowProps) =>
      useComposerQueue({
        activeQueueSessionKey: props.sessionKey,
        attachments: [],
        busy: props.busy,
        clearDraft: () => {},
        draftRef,
        focusInput: () => {},
        loadIntoComposer: () => {},
        onCancel,
        onSubmit,
        queueEditRef,
        queueSessionKey: undefined,
        sessionId: props.sessionKey
      }),
    { initialProps: { busy, sessionKey: SESSION_KEY } }
  )

  return { ...rendered, queueEditRef }
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
    window.localStorage.removeItem(QUEUE_STORAGE_KEY)

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

  it('purges (never resubmits) an entry whose sent claim is held by a broken-storage window', async () => {
    const restore = installFakeLocks()

    try {
      const entry = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'sent by broken window' })

      // A window whose removals cannot reach storage marked its send with a
      // held Web Lock instead.
      markQueuedPromptSent(entry!.id)
      await Promise.resolve()

      const onSubmit = vi.fn(() => Promise.resolve(true))
      const { unmount } = renderWindow(onSubmit)
      await act(async () => {})

      expect(onSubmit).not.toHaveBeenCalled()
      await waitFor(() => expect(persistedQueueTexts(SESSION_KEY)).toEqual([]))
      unmount()
    } finally {
      restore()
    }
  })

  it('a real successful drain marks its entry sent, protecting it even if tombstones are lost', async () => {
    const restore = installFakeLocks()

    try {
      const entry = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'sent for real' })

      // Window A drains the entry through the real flow (this is what must
      // call markQueuedPromptSent — the producer half of the protection).
      const onSubmitA = vi.fn(() => Promise.resolve(true))
      const windowA = renderWindow(onSubmitA)
      await act(async () => {})

      expect(onSubmitA).toHaveBeenCalledTimes(1)

      // Simulate total loss of the storage-side protection: tombstones wiped
      // and the entry resurrected by a stale save (event included, so the
      // atom adopts the resurrected entry like a real cross-window write).
      window.localStorage.removeItem(QUEUE_TOMBSTONES_STORAGE_KEY)
      const resurrected = JSON.stringify({ [SESSION_KEY]: [entry] })
      window.localStorage.setItem(QUEUE_STORAGE_KEY, resurrected)
      window.dispatchEvent(new StorageEvent('storage', { key: QUEUE_STORAGE_KEY, newValue: resurrected }))

      // A fresh idle window must purge the entry via A's held sent claim, not
      // submit it a second time.
      const onSubmitB = vi.fn(() => Promise.resolve(true))
      const windowB = renderWindow(onSubmitB)
      await act(async () => {})

      expect(onSubmitB).not.toHaveBeenCalled()
      await waitFor(() => expect(persistedQueueTexts(SESSION_KEY)).toEqual([]))

      windowA.unmount()
      windowB.unmount()
    } finally {
      restore()
    }
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

  it('a manual send-now waits for ANOTHER window’s in-flight drain instead of silently dropping the tap', async () => {
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
      // wait for A's claim, not vanish. Marking the entry as "being edited" in
      // B's ref keeps B's own auto-drain off it, isolating the manual path.
      const onSubmitB = vi.fn(() => Promise.resolve(true))
      const windowB = renderWindow(onSubmitB)
      windowB.queueEditRef.current = {
        attachments: [],
        draft: '',
        entryId: second!.id,
        sessionKey: SESSION_KEY
      } as QueueEditState
      await act(async () => {})

      windowB.queueEditRef.current = null

      let sendResolved: null | boolean = null
      let sendNow!: Promise<boolean>

      await act(async () => {
        sendNow = windowB.result.current.sendQueuedNow(second!.id)
        void sendNow.then(sent => (sendResolved = sent))
      })

      expect(sendResolved).toBeNull()
      expect(onSubmitB).not.toHaveBeenCalled()

      // A's drain completes; B's waiting tap acquires the claim and sends the
      // exact entry the user tapped.
      await act(async () => {
        settleA(true)
      })

      await waitFor(() => expect(onSubmitB).toHaveBeenCalledWith('tapped entry', { attachments: [], fromQueue: true }))
      await expect(sendNow).resolves.toBe(true)
      expect(vi.mocked(notify)).not.toHaveBeenCalled()

      windowA.unmount()
      windowB.unmount()
    } finally {
      restore()
    }
  })

  it('a manual send-now during THIS window’s own in-flight drain waits too (no false “busy elsewhere”)', async () => {
    const restore = installFakeLocks()

    try {
      enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'auto head' })
      const tapped = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'tapped entry' })

      let settleAuto!: (accepted: boolean) => void

      const onSubmit = vi
        .fn<OnSubmit>()
        .mockImplementationOnce(() => new Promise<boolean>(resolve => (settleAuto = resolve)))
        .mockImplementation(() => Promise.resolve(true))

      const win = renderWindow(onSubmit)
      // Keep this window's auto-drain off the tapped entry so the manual path
      // owns it deterministically.
      win.queueEditRef.current = {
        attachments: [],
        draft: '',
        entryId: tapped!.id,
        sessionKey: SESSION_KEY
      } as QueueEditState
      await act(async () => {})

      expect(onSubmit).toHaveBeenCalledTimes(1) // auto-drain of the head, in flight

      win.queueEditRef.current = null

      let sendNow!: Promise<boolean>
      await act(async () => {
        sendNow = win.result.current.sendQueuedNow(tapped!.id)
      })

      // Still waiting on the LOCAL in-flight drain — not bounced, not toasted.
      expect(onSubmit).toHaveBeenCalledTimes(1)

      await act(async () => {
        settleAuto(true)
      })

      await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('tapped entry', { attachments: [], fromQueue: true }))
      await expect(sendNow).resolves.toBe(true)
      expect(vi.mocked(notify)).not.toHaveBeenCalled()

      win.unmount()
    } finally {
      restore()
    }
  })

  it('a manual send-now that outwaits its budget resolves false and says the queue is busy', async () => {
    // failWaits: waiting requests reject as a timed-out AbortSignal would,
    // without spending the real 15s budget.
    const restore = installFakeLocks({ failWaits: true })

    try {
      const entry = enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'blocked tap' })

      // Window A holds the claim forever.
      const onSubmitA = vi.fn(() => new Promise<boolean>(() => {}))
      const windowA = renderWindow(onSubmitA)
      await act(async () => {})

      expect(onSubmitA).toHaveBeenCalledTimes(1)

      const onSubmitB = vi.fn(() => Promise.resolve(true))
      const windowB = renderWindow(onSubmitB, { busy: true }) // busy: no auto-drain noise
      windowB.rerender({ busy: false, sessionKey: SESSION_KEY })
      await act(async () => {})

      await act(async () => {
        await expect(windowB.result.current.sendQueuedNow(entry!.id)).resolves.toBe(false)
      })

      expect(onSubmitB).not.toHaveBeenCalled()
      expect(vi.mocked(notify)).toHaveBeenCalledWith(
        expect.objectContaining({ id: 'composer-queue-busy-elsewhere' })
      )

      windowA.unmount()
      windowB.unmount()
    } finally {
      restore()
    }
  })

  it('retries a rejected auto-drain with backoff until it sends (bounded)', async () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'flaky send' })

    // First two submits are rejected, the third succeeds — inside
    // MAX_AUTO_DRAIN_ATTEMPTS. Without the retry timer nothing would ever
    // re-run auto-drain: the effect deps do not change on a rejected send.
    let attempts = 0
    const onSubmit = vi.fn(() => Promise.resolve(++attempts >= 3))

    const { unmount } = renderWindow(onSubmit)
    await act(async () => {})

    expect(onSubmit).toHaveBeenCalledTimes(1)

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(3), { timeout: 4_000 })
    await waitFor(() => expect(getQueuedPrompts(SESSION_KEY)).toEqual([]))

    expect(vi.mocked(notify)).not.toHaveBeenCalled()
    unmount()
  })

  it('stops at the retry cap and raises the stuck-queue toast exactly once', async () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'never sends' })

    const onSubmit = vi.fn(() => Promise.resolve(false))
    const { unmount } = renderWindow(onSubmit)
    await act(async () => {})

    // MAX_AUTO_DRAIN_ATTEMPTS = 4: the initial attempt plus three backoff
    // retries, then the toast — and NO further attempts.
    await waitFor(() => expect(vi.mocked(notify)).toHaveBeenCalledWith(expect.objectContaining({ id: 'composer-queue-stuck' })), {
      timeout: 4_000
    })
    expect(onSubmit).toHaveBeenCalledTimes(4)

    await new Promise(resolve => setTimeout(resolve, 250))
    expect(onSubmit).toHaveBeenCalledTimes(4)
    expect(vi.mocked(notify)).toHaveBeenCalledTimes(1)

    // The entry stays queued for a manual send.
    expect(getQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['never sends'])
    unmount()
  })

  it('a tapped entry that no longer exists does not interrupt a live turn', async () => {
    const onCancel = vi.fn()
    const onSubmit = vi.fn(() => Promise.resolve(true))
    const { result, unmount } = renderWindow(onSubmit, { busy: true, onCancel })

    await act(async () => {
      await expect(result.current.sendQueuedNow('queued-phantom-id')).resolves.toBe(false)
    })

    expect(onCancel).not.toHaveBeenCalled()
    unmount()
  })
})

describe('useComposerQueue unmount safety', () => {
  beforeEach(() => {
    vi.mocked(notify).mockClear()
    resetQueueStorage()
  })

  it('an armed claim-release waiter does not drain after its window unmounts', async () => {
    const restore = installFakeLocks()

    try {
      enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'not for the dead window' })

      let settleA!: (accepted: boolean) => void
      const onSubmitA = vi.fn(() => new Promise<boolean>(resolve => (settleA = resolve)))
      const windowA = renderWindow(onSubmitA)
      await act(async () => {})

      const onSubmitB = vi.fn(() => Promise.resolve(true))
      const windowB = renderWindow(onSubmitB)
      await act(async () => {}) // B contends and arms its claim-release waiter

      windowB.unmount()

      await act(async () => {
        settleA(false) // release with no send: the waiter fires…
      })
      await new Promise(resolve => setTimeout(resolve, 50))

      // …but B is unmounted, so it must not drain.
      expect(onSubmitB).not.toHaveBeenCalled()
      expect(getQueuedPrompts(SESSION_KEY).map(e => e.text)).toEqual(['not for the dead window'])

      windowA.unmount()
    } finally {
      restore()
    }
  })

  it('a pending backoff retry is cancelled by unmount (timer actually cleared, not just guarded)', async () => {
    enqueueQueuedPrompt(SESSION_KEY, { attachments: [], text: 'no zombie retries' })

    const setSpy = vi.spyOn(globalThis, 'setTimeout')
    const clearSpy = vi.spyOn(globalThis, 'clearTimeout')

    try {
      const onSubmit = vi.fn(() => Promise.resolve(false))
      const { unmount } = renderWindow(onSubmit)
      await act(async () => {})

      expect(onSubmit).toHaveBeenCalledTimes(1) // rejected; retry timer armed

      // Pin the clearTimeout itself — the mountedRef guard alone would also
      // suppress the submit, so counting submits cannot distinguish them.
      // The retry timer is the setTimeout armed with the mocked backoff
      // (AUTO_DRAIN_RETRY_BASE_MS × 1 = 20ms).
      const retryIndex = setSpy.mock.calls.findIndex(call => call[1] === 20)

      expect(retryIndex).toBeGreaterThanOrEqual(0)

      const retryHandle = setSpy.mock.results[retryIndex]!.value as ReturnType<typeof setTimeout>

      unmount()
      expect(clearSpy).toHaveBeenCalledWith(retryHandle)

      await new Promise(resolve => setTimeout(resolve, 100))
      expect(onSubmit).toHaveBeenCalledTimes(1)
    } finally {
      setSpy.mockRestore()
      clearSpy.mockRestore()
    }
  })
})

describe('useComposerQueue runtime re-key migration', () => {
  beforeEach(() => {
    vi.mocked(notify).mockClear()
    resetQueueStorage()
  })

  it('chains two quick re-keys so entries land under the final key even when the first move must wait', async () => {
    const restore = installFakeLocks()

    try {
      enqueueQueuedPrompt('rt-1', { attachments: [], text: 'follow me' })

      // A drain claim is held on rt-1 (an in-flight submit under the old key),
      // so the rt-1 → rt-2 migration must WAIT. Without chaining, the
      // rt-2 → rt-3 migration would run first (moving nothing) and the entry
      // would strand under the dead intermediate key rt-2 forever.
      let releaseDrain!: () => void
      const heldDrain = withSessionDrainClaim('rt-1', () => new Promise<void>(resolve => (releaseDrain = resolve)))
      await Promise.resolve()

      const onSubmit = vi.fn(() => Promise.resolve(true))
      const draftRef: RefObject<string> = { current: '' }
      const queueEditRef: RefObject<QueueEditState | null> = { current: null }

      const { rerender, unmount } = renderHook(
        (props: { sessionKey: string }) =>
          useComposerQueue({
            activeQueueSessionKey: props.sessionKey,
            attachments: [],
            busy: true, // keep auto-drain out of the way; migration is the subject
            clearDraft: () => {},
            draftRef,
            focusInput: () => {},
            loadIntoComposer: () => {},
            onCancel: () => {},
            onSubmit,
            queueEditRef,
            queueSessionKey: undefined,
            sessionId: props.sessionKey
          }),
        { initialProps: { sessionKey: 'rt-1' } }
      )

      // Two re-keys in quick succession, no flush in between.
      rerender({ sessionKey: 'rt-2' })
      rerender({ sessionKey: 'rt-3' })
      await act(async () => {})

      // Both migrations are queued behind the held rt-1 claim; nothing moved.
      expect(persistedQueueTexts('rt-1')).toEqual(['follow me'])

      releaseDrain()
      await heldDrain

      await waitFor(() => expect(persistedQueueTexts('rt-3')).toEqual(['follow me']))
      expect(persistedQueueTexts('rt-1')).toEqual([])
      expect(persistedQueueTexts('rt-2')).toEqual([])

      unmount()
    } finally {
      restore()
    }
  })
})
