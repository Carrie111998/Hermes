/**
 * Composer focus + external-insert bus.
 *
 * Mutations from outside the composer (sidebar attach, drag drop, terminal
 * Cmd+L, preview console, etc.) dispatch through here. Each composer subscribes
 * and routes the work back into its own ref/state.
 *
 * `dispatch` defers to a macrotask so synchronous click/keydown handlers
 * (react-arborist row focus, picker `node.select()`) finish first and don't
 * steal focus from the composer effect.
 */

import { queryAllVisible, queryVisible } from '@/components/pane-shell/pane-visibility'
import { $hoveredTreeGroup } from '@/components/pane-shell/tree/store'

import type { InlineRefInput } from './inline-refs'
import { RICH_INPUT_SLOT } from './rich-editor'

/** Composer routing key. The main chat is `'main'`, the edit composer
 *  `'edit'`; scoped composers (session tiles) use `'tile:<id>'`. */
export type ComposerTarget = 'edit' | 'main' | (string & {})
export type ComposerInsertMode = 'block' | 'inline' | 'prefix'

/** One mounted composer. `surfaceId` is the useId-backed ChatView token. */
export interface ComposerAddress {
  surfaceId?: string
  target: ComposerTarget
}

export interface ComposerSurfaceAddress extends ComposerAddress {
  surfaceId: string
}

export interface FocusDetail extends ComposerAddress {
  /** Append after focus (type-to-focus / soft `/`). */
  typeChar?: string
}

interface InsertDetail extends ComposerAddress {
  mode: ComposerInsertMode
  text: string
}

interface InsertRefsDetail extends ComposerAddress {
  refs: InlineRefInput[]
}

interface AttachImagesDetail extends ComposerAddress {
  blobs: Blob[]
}

const FOCUS_EVENT = 'hermes:composer-focus'
const INSERT_EVENT = 'hermes:composer-insert'
const ATTACH_IMAGES_EVENT = 'hermes:composer-attach-images'
const INSERT_REFS_EVENT = 'hermes:composer-insert-refs'
const SUBMIT_EVENT = 'hermes:composer-submit'
const VOICE_TOGGLE_EVENT = 'hermes:composer-voice-toggle'
const MODEL_MENU_EVENT = 'hermes:composer-model-menu'

/** Inline edit composer root — mounted only while a user bubble is being edited. */
const EDIT_COMPOSER_ROOT = '[data-slot="aui_edit-composer-root"]'

/** Attribute-safe selector fragment. jsdom (vitest) does not ship `CSS.escape`. */
const cssEscape = (value: string): string => {
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
    return CSS.escape(value)
  }

  // Our targets are `'main'` / `'edit'` / `'tile:<id>'` — alphanumerics plus `:`
  // and `-`. Escape anything outside that set so a weird id cannot break the
  // attribute selector.
  return value.replace(/[^a-zA-Z0-9_:-]/g, ch => `\\${ch}`)
}

interface SubmitDetail {
  /** Unique mounted composer surface captured at click time. */
  surfaceId: string
  target: ComposerTarget
  text: string
  /** `hidden` types the persisted user row so no bubble renders — the
   *  off-screen path for widget intents. Omit for normal visible sends. */
  displayKind?: 'hidden'
}

let activeComposer: ComposerAddress = { target: 'main' }

const surfaceAddress = (surface: HTMLElement | null): ComposerSurfaceAddress | null => {
  const target = surface?.dataset.composerTarget
  const surfaceId = surface?.dataset.composerSurfaceId

  return target && surfaceId ? { surfaceId, target: target as ComposerTarget } : null
}

/** The first visible chat surface, used only when it is the sole fallback. */
const visibleChatAddress = (): ComposerAddress | null => {
  if (typeof document === 'undefined') {
    return null
  }

  const surfaces = queryAllVisible<HTMLElement>('[data-composer-target]')

  if (surfaces.length !== 1) {
    return null
  }

  return surfaceAddress(surfaces[0]) ?? {
    target: surfaces[0]?.dataset.composerTarget as ComposerTarget
  }
}

const uniqueVisibleAddress = (target: ComposerTarget): ComposerAddress | null => {
  if (typeof document === 'undefined') {
    return null
  }

  const surfaces = queryAllVisible<HTMLElement>(`[data-composer-target="${cssEscape(target)}"]`)

  return surfaces.length === 1 ? (surfaceAddress(surfaces[0]) ?? { target }) : null
}

/** True when an exact composer claim is still live and on screen. */
const addressIsReachable = ({ surfaceId, target }: ComposerAddress): boolean => {
  if (typeof document === 'undefined') {
    return true
  }

  if (target === 'edit') {
    const root = document.querySelector<HTMLElement>(EDIT_COMPOSER_ROOT)

    if (!root) {
      return false
    }

    return !surfaceId || root.dataset.composerSurfaceId === surfaceId
  }

  const visible = queryAllVisible<HTMLElement>(`[data-composer-target="${cssEscape(target)}"]`)

  if (surfaceId) {
    if (visible.some(surface => surface.dataset.composerSurfaceId === surfaceId)) {
      return true
    }

    // Before ChatView stamps the DOM (and in pure hook tests), retain the exact
    // claim. A different visible surface is the only proof it went stale.
    return !queryVisible('[data-composer-target]')
  }

  if (visible.length > 0) {
    return true
  }

  // A different chat surface is on screen → this claim is buried or gone.
  // With no stamped surfaces yet (first paint / pure unit tests), retain it.
  return !queryVisible('[data-composer-target]')
}

/** Resolve `'active'` without ever choosing arbitrarily between visible twins. */
const resolveActive = (): ComposerAddress => {
  if (addressIsReachable(activeComposer)) {
    return activeComposer
  }

  activeComposer = visibleChatAddress() ?? { target: 'main' }

  return activeComposer
}

const resolve = (target: ComposerTarget | 'active', surfaceId?: string): ComposerAddress => {
  if (target === 'active') {
    return resolveActive()
  }

  return surfaceId ? { surfaceId, target } : (uniqueVisibleAddress(target) ?? { target })
}

const dispatch = <T>(name: string, detail: T) => {
  if (typeof window === 'undefined') {
    return
  }

  window.setTimeout(() => window.dispatchEvent(new CustomEvent<T>(name, { detail })), 0)
}

/** Submit is the one bus mutation that must preserve the chat visible at click
 * time. Deferring it lets a parent click handler/tab reveal switch the active
 * keep-alive pane before subscribers run, so the task is dropped or claimed by
 * another composer. Other bus events intentionally defer for focus restoration.
 */
const dispatchNow = <T>(name: string, detail: T) => {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent<T>(name, { detail }))
  }
}

/** Unique identity for the sole visible composer matching a legacy target. */
const getVisibleComposerSurfaceId = (target: ComposerTarget): string | null => {
  if (typeof document === 'undefined') {
    return null
  }

  const surfaces = queryAllVisible<HTMLElement>(`[data-composer-target="${cssEscape(target)}"]`)

  return surfaces.length === 1 ? surfaces[0]?.dataset.composerSurfaceId || null : null
}

const composerSurfaceIsVisible = (target: ComposerTarget, surfaceId: string): boolean => {
  if (typeof document === 'undefined') {
    return false
  }

  return queryAllVisible<HTMLElement>(`[data-composer-target="${cssEscape(target)}"]`).some(
    surface => surface.dataset.composerSurfaceId === surfaceId
  )
}

type AddressedDetail = ComposerAddress

type DetailHandler<T> = (detail: T) => void

/** Exact subscribers are counted so a legacy target-only request can be
 * delivered only when that target has one possible surface. */
const exactSubscriberCounts = new Map<string, Map<ComposerTarget, Map<string, number>>>()

const adjustExactSubscriber = (name: string, address: ComposerSurfaceAddress, delta: 1 | -1) => {
  const byTarget = exactSubscriberCounts.get(name) ?? new Map<ComposerTarget, Map<string, number>>()
  const bySurface = byTarget.get(address.target) ?? new Map<string, number>()
  const next = (bySurface.get(address.surfaceId) ?? 0) + delta

  if (next > 0) {
    bySurface.set(address.surfaceId, next)
  } else {
    bySurface.delete(address.surfaceId)
  }

  if (bySurface.size > 0) {
    byTarget.set(address.target, bySurface)
  } else {
    byTarget.delete(address.target)
  }

  if (byTarget.size > 0) {
    exactSubscriberCounts.set(name, byTarget)
  } else {
    exactSubscriberCounts.delete(name)
  }
}

const requestMatches = (name: string, address: ComposerSurfaceAddress, detail: AddressedDetail): boolean => {
  if (detail.target !== address.target) {
    return false
  }

  if (detail.surfaceId) {
    return detail.surfaceId === address.surfaceId
  }

  const possible = exactSubscriberCounts.get(name)?.get(address.target)

  return possible?.size === 1 && possible.has(address.surfaceId)
}

const subscribe = <T extends AddressedDetail>(
  name: string,
  addressOrHandler: ComposerSurfaceAddress | DetailHandler<T>,
  maybeHandler?: DetailHandler<T>
) => {
  if (typeof window === 'undefined') {
    return () => undefined
  }

  const address = typeof addressOrHandler === 'function' ? null : addressOrHandler
  const handler = (typeof addressOrHandler === 'function' ? addressOrHandler : maybeHandler) as DetailHandler<T>

  if (address) {
    adjustExactSubscriber(name, address, 1)
  }

  const listener = (event: Event) => {
    const detail = (event as CustomEvent<T>).detail

    if (detail && (!address || requestMatches(name, address, detail))) {
      handler(detail)
    }
  }

  window.addEventListener(name, listener)

  return () => {
    window.removeEventListener(name, listener)

    if (address) {
      adjustExactSubscriber(name, address, -1)
    }
  }
}

export const markActiveComposer = (target: ComposerTarget, surfaceId?: string) => {
  activeComposer = surfaceId ? { surfaceId, target } : (uniqueVisibleAddress(target) ?? { target })
}

/** Hand an exact routing claim back when its composer unmounts. */
export const releaseActiveComposer = (target: ComposerTarget, surfaceId?: string) => {
  if (activeComposer.target !== target || (activeComposer.surfaceId && activeComposer.surfaceId !== surfaceId)) {
    return
  }

  activeComposer = visibleChatAddress() ?? { target: 'main' }
}

/** Backward-compatible target view of the active exact address. */
export const getActiveComposer = (): ComposerTarget => resolveActive().target

export const getActiveComposerAddress = (): ComposerAddress => resolveActive()

export const requestComposerFocus = (
  target: ComposerTarget | 'active' = 'active',
  { surfaceId, typeChar }: { surfaceId?: string; typeChar?: string } = {}
) => dispatch<FocusDetail>(FOCUS_EVENT, { ...resolve(target, surfaceId), typeChar })

export const requestComposerInsert = (
  text: string,
  {
    mode = 'block',
    surfaceId,
    target = 'active'
  }: { mode?: ComposerInsertMode; surfaceId?: string; target?: ComposerTarget | 'active' } = {}
) => {
  const trimmed = text.trim()

  if (!trimmed) {
    return
  }

  dispatch<InsertDetail>(INSERT_EVENT, { ...resolve(target, surfaceId), mode, text: trimmed })
}

export const onComposerFocusRequest = (
  addressOrHandler: ComposerSurfaceAddress | DetailHandler<FocusDetail>,
  handler?: DetailHandler<FocusDetail>
) => subscribe(FOCUS_EVENT, addressOrHandler, handler)

export const onComposerInsertRequest = (
  addressOrHandler: ComposerSurfaceAddress | DetailHandler<InsertDetail>,
  handler?: DetailHandler<InsertDetail>
) => subscribe(INSERT_EVENT, addressOrHandler, handler)

/** Attach image blobs to a composer's attachment set — the unfocused-paste path. */
export const requestComposerAttachImages = (
  blobs: Blob[],
  { surfaceId, target = 'active' }: { surfaceId?: string; target?: ComposerTarget | 'active' } = {}
) => {
  if (blobs.length) {
    dispatch<AttachImagesDetail>(ATTACH_IMAGES_EVENT, { ...resolve(target, surfaceId), blobs })
  }
}

export const onComposerAttachImagesRequest = (
  addressOrHandler: ComposerSurfaceAddress | DetailHandler<AttachImagesDetail>,
  handler?: DetailHandler<AttachImagesDetail>
) => subscribe(ATTACH_IMAGES_EVENT, addressOrHandler, handler)

/** Insert typed ref chips (carrying a display label) into a composer. */
export const requestComposerInsertRefs = (
  refs: InlineRefInput[],
  { surfaceId, target = 'active' }: { surfaceId?: string; target?: ComposerTarget | 'active' } = {}
) => {
  if (refs.length) {
    dispatch<InsertRefsDetail>(INSERT_REFS_EVENT, { ...resolve(target, surfaceId), refs })
  }
}

export const onComposerInsertRefsRequest = (
  addressOrHandler: ComposerSurfaceAddress | DetailHandler<InsertRefsDetail>,
  handler?: DetailHandler<InsertRefsDetail>
) => subscribe(INSERT_REFS_EVENT, addressOrHandler, handler)

/** Submit a prompt through a composer as if the user typed + sent it. Lets
 * external panels (e.g. the review pane's "let the agent ship it" button) hand
 * the agent a task without the user round-tripping through the input. */
export const requestComposerSubmit = (
  text: string,
  {
    displayKind,
    surfaceId: requestedSurfaceId,
    target = 'active'
  }: { displayKind?: 'hidden'; surfaceId?: null | string; target?: ComposerTarget | 'active' } = {}
): boolean => {
  const trimmed = text.trim()

  if (!trimmed) {
    return false
  }

  const resolved = resolve(target, requestedSurfaceId ?? undefined)
  const resolvedTarget = resolved.target
  const surfaceId = requestedSurfaceId === undefined ? (resolved.surfaceId ?? getVisibleComposerSurfaceId(resolvedTarget)) : requestedSurfaceId

  // Fail closed: without an exact visible surface identity, broadcasting a
  // submit could make more than one keep-alive/new-chat composer claim it.
  if (!surfaceId || !composerSurfaceIsVisible(resolvedTarget, surfaceId)) {
    return false
  }

  dispatchNow<SubmitDetail>(SUBMIT_EVENT, {
    surfaceId,
    target: resolvedTarget,
    text: trimmed,
    ...(displayKind ? { displayKind } : {})
  })

  return true
}

export const onComposerSubmitRequest = (handler: (detail: SubmitDetail) => void) =>
  subscribe<SubmitDetail>(SUBMIT_EVENT, handler)

/** Toggle ONE composer's voice conversation. */
export const requestVoiceToggle = (
  target: ComposerTarget | 'active' = 'active',
  { surfaceId }: { surfaceId?: string } = {}
) => dispatch<ComposerAddress>(VOICE_TOGGLE_EVENT, resolve(target, surfaceId))

export const onComposerVoiceToggleRequest = (
  addressOrHandler: ComposerSurfaceAddress | ((target: ComposerTarget) => void),
  handler?: () => void
) => {
  if (typeof addressOrHandler === 'function') {
    return subscribe<ComposerAddress>(VOICE_TOGGLE_EVENT, detail => addressOrHandler(detail.target))
  }

  return subscribe<ComposerAddress>(VOICE_TOGGLE_EVENT, addressOrHandler, () => handler?.())
}

/** The exact chat surface inside the zone the pointer is over, if any. */
const composerAddressInHoveredZone = (): ComposerAddress | null => {
  const zone = $hoveredTreeGroup.get()

  if (!zone || typeof document === 'undefined') {
    return null
  }

  const surface = queryAllVisible<HTMLElement>('[data-composer-target]').find(
    el => el.closest<HTMLElement>('[data-tree-group]')?.dataset.treeGroup === zone
  )

  if (!surface?.dataset.composerTarget) {
    return null
  }

  return surfaceAddress(surface) ?? { target: surface.dataset.composerTarget as ComposerTarget }
}

/** Toggle ONE composer's model menu — the `composer.modelPicker` hotkey.
 *  Targets the pane under the pointer first (the tab-verb convention), then
 *  the active composer. Returns false when no chat surface is on screen at
 *  all (settings, profiles…), so the caller can fall back to the full
 *  model-picker dialog instead of dispatching into the void. */
export const requestModelMenuToggle = (): boolean => {
  if (typeof document !== 'undefined' && !queryVisible('[data-composer-target]')) {
    return false
  }

  dispatch<ComposerAddress>(MODEL_MENU_EVENT, composerAddressInHoveredZone() ?? resolveActive())

  return true
}

export const onComposerModelMenuRequest = (
  addressOrHandler: ComposerSurfaceAddress | ((target: ComposerTarget) => void),
  handler?: () => void
) => {
  if (typeof addressOrHandler === 'function') {
    return subscribe<ComposerAddress>(MODEL_MENU_EVENT, detail => addressOrHandler(detail.target))
  }

  return subscribe<ComposerAddress>(MODEL_MENU_EVENT, addressOrHandler, () => handler?.())
}

/**
 * Focus a composer input across React commit + browser focus restore.
 *
 * The triple-call survives:
 *   - sync: contenteditable already mounted
 *   - rAF:  React just committed a `renderComposerContents` swap
 *   - 0ms:  browser focus reclaim from a click target inside an external panel
 */
export const focusComposerInput = (el: HTMLElement | null, address?: ComposerSurfaceAddress) => {
  if (!el) {
    return
  }

  // Skip when already focused: focus() runs the full focusing steps (forcing
  // layout) even on the active element, and during a session switch the DOM is
  // large and dirty — the redundant retries were measurably expensive there.
  const focus = () => {
    if (address) {
      const active = resolveActive()

      // A click/focus on another same-target surface after the sync attempt
      // invalidates this request's rAF/timeout retries. Never steal it back.
      if (active.target !== address.target || active.surfaceId !== address.surfaceId) {
        return
      }
    }

    if (document.activeElement !== el) {
      el.focus({ preventScroll: true })
    }
  }

  focus()
  window.requestAnimationFrame(focus)
  window.setTimeout(focus, 0)
}

/** Drop focus from the main composer input (status-stack chrome, sidebar, etc.).
 *  Skips inactive tabs — they stay mounted, so an unscoped lookup can land on a
 *  background composer and leave the visible one focused. */
export const blurComposerInput = () => {
  const el = queryVisible(`[data-slot="${RICH_INPUT_SLOT}"]`)

  if (el && document.activeElement === el) {
    el.blur()
  }
}
