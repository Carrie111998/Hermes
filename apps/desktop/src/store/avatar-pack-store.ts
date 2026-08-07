/**
 * Avatar Pack store — reactive state for the avatar pack system.
 *
 * The renderer (pet-overlay-app.tsx) subscribes to these atoms to decide
 * whether to render a Petdex sprite or an Avatar Pack, which pack is selected,
 * and which state preview is active.
 *
 * The actual pack scanning happens on the main process side via IPC
 * (hermes:avatar-packs:list). This store holds the results + UI preferences.
 */

import { atom, computed } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'
import {
  type AvatarPackListResult,
  type AvatarRendererType,
  type AvatarState,
  type ResolvedAvatarPack
} from '@/store/avatar-pack-types'

// ── Renderer type guard ───────────────────────────────────────────────────────

/** Validate that a string is an AvatarRendererType. */
export function isValidRendererType(v: string): v is AvatarRendererType {
  return v === 'petdex' || v === 'avatar-pack'
}

// ── Persisted preferences ────────────────────────────────────────────────────

const RENDERER_TYPE_KEY = 'hermes.desktop.avatar-renderer-type.v1'
const SELECTED_PACK_KEY = 'hermes.desktop.avatar-selected-pack.v1'

export const $avatarRendererType = atom<AvatarRendererType>(
  ((): AvatarRendererType => {
    const v = storedString(RENDERER_TYPE_KEY)

    return v !== null && isValidRendererType(v) ? v : 'petdex'
  })()
)

export const $selectedAvatarPackId = atom<string | null>(
  ((): string | null => {
    const v = storedString(SELECTED_PACK_KEY)

    return v || null
  })()
)

$avatarRendererType.subscribe(v => persistString(RENDERER_TYPE_KEY, v))
$selectedAvatarPackId.subscribe(v => persistString(SELECTED_PACK_KEY, v ?? ''))

// ── Non-persisted runtime state ──────────────────────────────────────────────

/** Packs discovered by the last scan (from main process IPC). */
export const $avatarPacks = atom<AvatarPackListResult | null>(null)

/** Full resolved packs with asset URLs — loaded on demand from main process. */
export const $resolvedAvatarPacks = atom<ResolvedAvatarPack[]>([])

/**
 * Preview state for testing — when non-null, forces the renderer to show this
 * state instead of deriving from agent activity. Set by the settings preview
 * buttons. Cleared on null.
 */
export const $avatarPreviewState = atom<AvatarState | null>(null)

// ── Derived atoms ────────────────────────────────────────────────────────────

/** The currently selected resolved pack (null if none selected or not loaded). */
export const $selectedAvatarPack = computed(
  [$resolvedAvatarPacks, $selectedAvatarPackId],
  (packs, id): ResolvedAvatarPack | null => {
    if (!id) {
      return packs.find(p => p.assets.idle) ?? null
    }

    return packs.find(p => p.id === id) ?? null
  }
)

/** Whether the avatar pack system is the active renderer. */
export const $isAvatarPackMode = computed(
  $avatarRendererType,
  (type): boolean => type === 'avatar-pack'
)

// ── Setters ──────────────────────────────────────────────────────────────────

export function setAvatarRendererType(v: AvatarRendererType): void {
  $avatarRendererType.set(v)
}

export function setSelectedAvatarPackId(id: string | null): void {
  $selectedAvatarPackId.set(id)
}

export function setAvatarPreviewState(state: AvatarState | null): void {
  $avatarPreviewState.set(state)
}

/**
 * Map agent activity to an avatar state. Mirrors the priority in pet.ts's
 * derivePetState but maps to the 4 avatar states.
 *
 * State mapping (priority order, top wins):
 *   error / celebrate / justCompleted  → talk   (terminal/transient signals)
 *   awaitingInput                      → listen (P1 voice: agent needs input)
 *   toolRunning / reasoning            → think  (P1: tool calls, deep thought)
 *   busy                               → talk   (agent is streaming reply)
 *   (otherwise)                        → idle
 *
 * Notes for the SELECTIVE ADOPT mapping (2026-07-28):
 *   - The third-party hermes-desktop-avatar repo's 3-state machine (idle /
 *     thinking / talking) maps onto ours as: idle→idle, thinking→think,
 *     talking→talk. We added 'listen' for the P1 manual-mic / STT path which
 *     that repo did not model separately.
 *   - No real lip-sync: this is a render-state picker, not a mouth-shape
 *     driver. The talk state loops a prebuilt webp/webm/mp4 — animation is
 *     duration-agnostic, not synchronized to audio frames.
 *   - talk covers BOTH error/celebrate/completion (transient flashes) and
 *     streaming reply. That is intentional — they all read as "the avatar
 *     just said something", so showing the talk loop is the right cue.
 */
export function activityToAvatarState(activity: {
  error?: boolean
  celebrate?: boolean
  justCompleted?: boolean
  awaitingInput?: boolean
  toolRunning?: boolean
  reasoning?: boolean
  busy?: boolean
}): AvatarState {
  if (activity.error || activity.celebrate || activity.justCompleted) {
    return 'talk'
  }

  if (activity.awaitingInput) {
    return 'listen'
  }

  if (activity.toolRunning || activity.reasoning) {
    return 'think'
  }

  if (activity.busy) {
    return 'talk'
  }

  return 'idle'
}
