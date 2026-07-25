import { atom } from 'nanostores'

import type { AsyncDelegationRecord, DelegationAsyncListResponse, DelegationStatusResponse } from '../gatewayTypes.js'

export interface DelegationState {
  // Last known caps from `delegation.status` RPC.  null until fetched.
  maxConcurrentChildren: null | number
  maxSpawnDepth: null | number
  // True when spawning is globally paused (see tools/delegate_tool.py).
  paused: boolean
  // Monotonic clock of the last successful status fetch.
  updatedAt: null | number
}

const buildState = (): DelegationState => ({
  maxConcurrentChildren: null,
  maxSpawnDepth: null,
  paused: false,
  updatedAt: null
})

export const $delegationState = atom<DelegationState>(buildState())

export const getDelegationState = () => $delegationState.get()

export const patchDelegationState = (next: Partial<DelegationState>) =>
  $delegationState.set({ ...$delegationState.get(), ...next })

export const resetDelegationState = () => $delegationState.set(buildState())

// ── Overlay accordion open-state ──────────────────────────────────────
//
// Lifted out of OverlaySection's local useState so collapse choices
// survive:
//   - navigating to a different subagent (Detail remounts)
//   - switching list ↔ detail mode (Detail unmounts in list mode)
//   - walking history (←/→)
// Keyed by section title; missing entries fall back to the section's
// `defaultOpen` prop.

export const $overlaySectionsOpen = atom<Record<string, boolean>>({})

export const toggleOverlaySection = (title: string, defaultOpen: boolean) => {
  const state = $overlaySectionsOpen.get()
  const current = title in state ? state[title]! : defaultOpen

  $overlaySectionsOpen.set({ ...state, [title]: !current })
}

export const getOverlaySectionOpen = (title: string, defaultOpen: boolean): boolean => {
  const state = $overlaySectionsOpen.get()

  return title in state ? state[title]! : defaultOpen
}

/** Merge a raw RPC response into the store.  Tolerant of partial/omitted fields. */
export const applyDelegationStatus = (r: DelegationStatusResponse | null | undefined) => {
  if (!r) {
    return
  }

  const patch: Partial<DelegationState> = { updatedAt: Date.now() }

  if (typeof r.max_spawn_depth === 'number') {
    patch.maxSpawnDepth = r.max_spawn_depth
  }

  if (typeof r.max_concurrent_children === 'number') {
    patch.maxConcurrentChildren = r.max_concurrent_children
  }

  if (typeof r.paused === 'boolean') {
    patch.paused = r.paused
  }

  patchDelegationState(patch)
}

// ── Async-delegation snapshot (docked agents panel) ───────────────────
//
// Background delegations projected by the `delegation.async_list` RPC.
// Read-only mirror of the daemon-side registry; the panel merges these
// (background/done rows) with the live in-turn subagents from the turn
// store (running rows with live tool + elapsed).

export const $asyncDelegations = atom<AsyncDelegationRecord[]>([])

export const getAsyncDelegations = () => $asyncDelegations.get()

/**
 * Fields the panel actually renders. Two snapshots that agree on all of
 * them produce an identical frame, so re-setting the atom would repaint
 * the whole app for nothing.
 */
const sameRecord = (a: AsyncDelegationRecord, b: AsyncDelegationRecord) =>
  a.delegation_id === b.delegation_id &&
  a.status === b.status &&
  a.goal === b.goal &&
  a.role === b.role &&
  a.dispatched_at === b.dispatched_at &&
  a.completed_at === b.completed_at

const sameSnapshot = (a: AsyncDelegationRecord[], b: AsyncDelegationRecord[]) =>
  a.length === b.length && a.every((rec, i) => sameRecord(rec, b[i]!))

/**
 * Replace the async-delegation snapshot from a raw RPC response.
 *
 * The poll runs every 1.5s whether or not anything moved; most ticks are
 * byte-identical (finished rows are retained for a long time and running
 * rows only change on status transitions — elapsed is derived at render
 * time from `dispatched_at`, not carried in the record). Bail out when the
 * projection is unchanged so a quiet background delegation doesn't force
 * a full-app repaint 40 times a minute.
 */
export const applyAsyncList = (r: DelegationAsyncListResponse | null | undefined) => {
  const next = Array.isArray(r?.delegations) ? r!.delegations : []

  if (sameSnapshot($asyncDelegations.get(), next)) {
    return
  }

  $asyncDelegations.set(next)
}

export const resetAsyncDelegations = () => $asyncDelegations.set([])
