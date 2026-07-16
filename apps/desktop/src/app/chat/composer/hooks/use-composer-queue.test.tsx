import { act, renderHook } from '@testing-library/react'
import type { RefObject } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $queuedPromptsBySession, enqueueQueuedPrompt, getQueuedPrompts } from '@/store/composer-queue'

import type { QueueEditState } from '../composer-utils'

import { useComposerQueue } from './use-composer-queue'

vi.mock('@/lib/haptics', () => ({ triggerHaptic: () => {} }))
vi.mock('@/i18n', () => ({
  useI18n: () => ({ t: { composer: { queueStuckBody: 'stuck body', queueStuckTitle: 'stuck title' } } })
}))
vi.mock('@/store/notifications', () => ({ notify: () => {} }))
vi.mock('@/store/composer', () => ({ clearComposerAttachments: () => {} }))
vi.mock('@/store/composer-input-history', () => ({ resetBrowseState: () => {} }))
vi.mock('../composer-utils', () => ({
  cloneAttachments: (attachments: unknown[]) => attachments.map(a => ({ ...(a as object) }))
}))

const SESSION_KEY = 'session-drain'
const QUEUE_STORAGE_KEY = 'hermes.desktop.composerQueue.v1'

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

// Minimal Web Locks stand-in (jsdom has none) with real exclusivity +
// ifAvailable semantics, so two hook instances contend like two windows.
function installFakeLocks() {
  const held = new Set<string>()

  const request = async (
    name: string,
    options: { ifAvailable?: boolean },
    callback: (lock: null | { name: string }) => Promise<unknown>
  ): Promise<unknown> => {
    if (held.has(name)) {
      if (!options.ifAvailable) {
        throw new Error('fake lock manager only implements ifAvailable')
      }

      return callback(null)
    }

    held.add(name)

    try {
      return await callback({ name })
    } finally {
      held.delete(name)
    }
  }

  Object.defineProperty(window.navigator, 'locks', { configurable: true, value: { request } })

  return () => {
    delete (window.navigator as { locks?: unknown }).locks
  }
}

describe('useComposerQueue cross-window drain (#57516 review)', () => {
  beforeEach(() => {
    window.localStorage.removeItem(QUEUE_STORAGE_KEY)
    $queuedPromptsBySession.set({})
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
})
