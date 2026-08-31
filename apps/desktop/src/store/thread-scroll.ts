import { atom, type WritableAtom } from 'nanostores'

// "Is the thread parked at the bottom" is owned by use-stick-to-bottom inside
// ThreadMessageList (the scroll container). That state lives only in that
// subtree, so ThreadMessageList mirrors it into these atoms for the composer,
// status stack, and floating jump button — all of which render OUTSIDE the thread.
//
// Identity is per ChatView surface (`useComposerSurfaceId`), never process-global:
// each tiled session has its own composer + jump button + transcript scroller,
// and a shared atom/handler set would dim, jump, and scroll every visible tile
// when only one of them moved (#99271).
//
// `$threadScrolledUp` / `$threadJumpButtonVisible` remain the unscoped (legacy
// single-pane / test) slot. Surfaces that know their id use the scoped stores.

const DEFAULT_SCOPE = ''

function scopedBooleanAtom(
  slots: Map<string, WritableAtom<boolean>>,
  scope: string,
  initial: boolean
): WritableAtom<boolean> {
  let slot = slots.get(scope)

  if (!slot) {
    slot = atom(initial)
    slots.set(scope, slot)
  }

  return slot
}

const scrolledUpSlots = new Map<string, WritableAtom<boolean>>()
const jumpVisibleSlots = new Map<string, WritableAtom<boolean>>()

export function threadScrolledUpStore(scope: string = DEFAULT_SCOPE): WritableAtom<boolean> {
  return scopedBooleanAtom(scrolledUpSlots, scope, false)
}

export function threadJumpButtonVisibleStore(scope: string = DEFAULT_SCOPE): WritableAtom<boolean> {
  return scopedBooleanAtom(jumpVisibleSlots, scope, false)
}

export const $threadScrolledUp = threadScrolledUpStore()
export const $threadJumpButtonVisible = threadJumpButtonVisibleStore()

const setScoped = (target: WritableAtom<boolean>, value: boolean) => {
  if (target.get() !== value) {
    target.set(value)
  }
}

export const setThreadAtBottom = (isAtBottom: boolean, scope: string = DEFAULT_SCOPE) => {
  const scrolledUp = !isAtBottom

  setScoped(threadScrolledUpStore(scope), scrolledUp)
  setScoped(threadJumpButtonVisibleStore(scope), scrolledUp)
}

export const resetThreadScroll = (scope: string = DEFAULT_SCOPE) => setThreadAtBottom(true, scope)

type HandlerMap = Map<string, Set<() => void>>

function addHandler(map: HandlerMap, handler: () => void, scope: string): () => void {
  let handlers = map.get(scope)

  if (!handlers) {
    handlers = new Set()
    map.set(scope, handlers)
  }

  handlers.add(handler)

  return () => {
    handlers!.delete(handler)

    if (handlers!.size === 0) {
      map.delete(scope)
    }
  }
}

function fireHandlers(map: HandlerMap, scope: string) {
  map.get(scope)?.forEach(handler => handler())
}

// Cross-component bridge: the jump button lives by the composer, the viewport's
// `scrollToBottom` lives inside the thread. The bridge registers a handler; the
// button fires it. Mirrors the composer focus/insert emitter pattern.
const scrollToBottomHandlers: HandlerMap = new Map()

export const onScrollToBottomRequest = (handler: () => void, scope: string = DEFAULT_SCOPE) =>
  addHandler(scrollToBottomHandlers, handler, scope)

export const requestScrollToBottom = (scope: string = DEFAULT_SCOPE) => fireHandlers(scrollToBottomHandlers, scope)

// Inline edit grows a sticky human bubble. Fire on pointerdown so the viewport
// escapes stick-to-bottom before focus/layout; close clears the edit flag when
// the inline composer unmounts.
const editOpenHandlers: HandlerMap = new Map()
const editCloseHandlers: HandlerMap = new Map()

export const onThreadEditOpen = (handler: () => void, scope: string = DEFAULT_SCOPE) =>
  addHandler(editOpenHandlers, handler, scope)

export const notifyThreadEditOpen = (scope: string = DEFAULT_SCOPE) => fireHandlers(editOpenHandlers, scope)

export const onThreadEditClose = (handler: () => void, scope: string = DEFAULT_SCOPE) =>
  addHandler(editCloseHandlers, handler, scope)

export const notifyThreadEditClose = (scope: string = DEFAULT_SCOPE) => fireHandlers(editCloseHandlers, scope)
