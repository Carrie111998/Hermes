import { atom, type WritableAtom } from 'nanostores'

// "Is the thread parked at the bottom" is owned by use-stick-to-bottom inside
// ThreadMessageList (the scroll container). That state lives only in that
// subtree, so ThreadMessageList mirrors it into these atoms for the composer,
// status stack, and floating jump button — all of which render OUTSIDE the thread.
//
// `$threadScrolledUp` dims the composer / status stack; `$threadJumpButtonVisible`
// shows the floating jump control. Both track `!isAtBottom` today, but stay
// separate so their thresholds can diverge again without touching consumers.
export const $threadScrolledUp = atom(false)
export const $threadJumpButtonVisible = atom(false)

// Skip no-op writes so subscribers don't churn on every scroll tick.
const setter = (target: WritableAtom<boolean>) => (value: boolean) => {
  if (target.get() !== value) {
    target.set(value)
  }
}

const setScrolledUp = setter($threadScrolledUp)
const setJumpButtonVisible = setter($threadJumpButtonVisible)

export const setThreadAtBottom = (isAtBottom: boolean) => {
  setScrolledUp(!isAtBottom)
  setJumpButtonVisible(!isAtBottom)
}

export const resetThreadScroll = () => setThreadAtBottom(true)

// Cross-component bridge: the jump button lives by the composer, the viewport's
// `scrollToBottom` lives inside the thread. The bridge registers a handler; the
// button fires it. Mirrors the composer focus/insert emitter pattern.
const handlers = new Set<() => void>()

export const onScrollToBottomRequest = (handler: () => void) => {
  handlers.add(handler)

  return () => void handlers.delete(handler)
}

export const requestScrollToBottom = () => handlers.forEach(handler => handler())

// Fixed-duration eased jump shared by the prompt timeline and message-local
// answer navigation. Native smooth scrolling scales with distance and crawls
// across long sessions. Track animation state per viewport so split panes never
// cancel each other's jump.
const jumpRafs = new WeakMap<HTMLElement, number>()

export function jumpThreadScroll(viewport: HTMLElement, top: number, duration = 170): void {
  const activeRaf = jumpRafs.get(viewport)

  if (activeRaf !== undefined) {
    cancelAnimationFrame(activeRaf)
  }

  const start = viewport.scrollTop
  const delta = top - start
  const prefersReducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false

  if (prefersReducedMotion || Math.abs(delta) < 2) {
    viewport.scrollTop = top
    jumpRafs.delete(viewport)

    return
  }

  const startedAt = performance.now()
  const easeOutCubic = (progress: number) => 1 - (1 - progress) ** 3

  const step = (now: number) => {
    const progress = Math.min(1, (now - startedAt) / duration)
    viewport.scrollTop = start + delta * easeOutCubic(progress)

    if (progress < 1) {
      jumpRafs.set(viewport, requestAnimationFrame(step))
    } else {
      jumpRafs.delete(viewport)
    }
  }

  jumpRafs.set(viewport, requestAnimationFrame(step))
}

// Message-local answer navigation stays DOM-local rather than joining the
// global handler set above. A bubbling event reaches only the transcript
// viewport that owns the clicked prompt, so split/grid panes cannot scroll one
// another when two sessions happen to contain the same message id.
const SCROLL_TO_ANSWER_START_EVENT = 'hermes:scroll-to-answer-start'

export const requestScrollToAnswerStart = (source: HTMLElement, messageId: string) => {
  source.dispatchEvent(
    new CustomEvent(SCROLL_TO_ANSWER_START_EVENT, {
      bubbles: true,
      detail: { messageId }
    })
  )
}

export const onScrollToAnswerStartRequest = (element: HTMLElement, handler: (messageId: string) => void) => {
  const listener = (event: Event) => {
    const messageId = (event as CustomEvent<{ messageId?: unknown }>).detail?.messageId

    if (typeof messageId === 'string' && messageId) {
      handler(messageId)
    }
  }

  element.addEventListener(SCROLL_TO_ANSWER_START_EVENT, listener)

  return () => element.removeEventListener(SCROLL_TO_ANSWER_START_EVENT, listener)
}

// Inline edit grows a sticky human bubble. Fire on pointerdown so the viewport
// escapes stick-to-bottom before focus/layout; close clears the edit flag when
// the inline composer unmounts.
const editOpenHandlers = new Set<() => void>()
const editCloseHandlers = new Set<() => void>()

export const onThreadEditOpen = (handler: () => void) => {
  editOpenHandlers.add(handler)

  return () => void editOpenHandlers.delete(handler)
}

export const notifyThreadEditOpen = () => editOpenHandlers.forEach(handler => handler())

export const onThreadEditClose = (handler: () => void) => {
  editCloseHandlers.add(handler)

  return () => void editCloseHandlers.delete(handler)
}

export const notifyThreadEditClose = () => editCloseHandlers.forEach(handler => handler())
