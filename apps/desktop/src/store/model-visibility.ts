import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'
import type { ModelOptionProvider } from '@/types/hermes'

import { $gateway } from './gateway'
import { $activeGatewayProfile, normalizeProfileKey } from './profile'

const STORAGE_KEY = 'hermes.desktop.visible-models'

/** Models shown per provider in the status-bar dropdown before the user has
 *  customized the list. Backend `models` are already relevance-ordered. */
export const DEFAULT_VISIBLE_PER_PROVIDER = 50

/** Stable key for a provider/model pair (`::` avoids colliding with model ids
 *  that contain a single colon, e.g. `model:tag`). */
export const modelVisibilityKey = (provider: string, model: string): string => `${provider}::${model}`

/** Sentinel key suffix stored when the user explicitly hides ALL models for a
 *  provider.  Distinguishes "user hid everything" from "never customized" so
 *  `effectiveVisibleKeys` does not re-add defaults for that provider. */
export const EMPTY_PROVIDER_SENTINEL = ''

/** Build the sentinel key for a provider whose last model was toggled off. */
export const emptyProviderSentinelKey = (provider: string): string =>
  modelVisibilityKey(provider, EMPTY_PROVIDER_SENTINEL)

/** Check whether a stored key is a provider-hidden sentinel. */
export const isProviderSentinel = (key: string): boolean => key.endsWith('::')

/** A model and its optional `…-fast` sibling, collapsed into one logical row.
 *  `id` is the canonical (base) model; `fastId` is the fast variant if present. */
export interface ModelFamily {
  fastId: string | null
  id: string
}

/** Collapse a provider's model list so a base model and its `…-fast` variant
 *  become a single family (one row, one toggle). Order is preserved by the
 *  base model's position. A `…-fast` model with no base stands on its own. */
export function collapseModelFamilies(models: readonly string[]): ModelFamily[] {
  const present = new Set(models)
  const families: ModelFamily[] = []
  const consumed = new Set<string>()

  for (const model of models) {
    if (consumed.has(model)) {
      continue
    }

    if (/-fast$/i.test(model) && present.has(model.replace(/-fast$/i, ''))) {
      // Represented by its base entry — the base attaches it as `fastId`.
      continue
    }

    if (/-\d{8}$/.test(model) && present.has(model.replace(/-\d{8}$/, ''))) {
      // A date-pinned snapshot superseded by its rolling alias — drop the dupe.
      continue
    }

    const fastId = `${model}-fast`
    const hasFast = present.has(fastId)
    families.push({ fastId: hasFast ? fastId : null, id: model })
    consumed.add(model)

    if (hasFast) {
      consumed.add(fastId)
    }
  }

  return families
}

// ── Backend persistence ──────────────────────────────────────────────
// Model visibility preferences are persisted on the backend so they survive
// cache clears, origin changes, and Electron userData resets.
//
// Profile scoping: model visibility is per-profile, so every RPC carries the
// active gateway profile (the gateway no-ops it for the launch profile) — one
// chokepoint so a call site can't forget it, mirroring the pet store's petRpc.

/** Profile the active window backend is scoped to (normalized). */
function visibilityProfile(): string {
  return normalizeProfileKey($activeGatewayProfile.get())
}

/** Serialize every backend write. Each save snapshots the atom at call time so
 *  two rapid toggles can't complete out of order and persist a stale selection
 *  (the server's last write wins only for the *latest* intent). */
let saveChain: Promise<void> = Promise.resolve()

function enqueueBackendSave(keys: Set<string>): void {
  const gateway = $gateway.get()

  if (!gateway) {return}
  const snapshot = [...keys]
  saveChain = saveChain.then(async () => {
    try {
      await gateway.request('model_visibility.set', {
        keys: snapshot,
        profile: visibilityProfile()
      })
    } catch {
      // Best-effort; localStorage remains the offline fallback.
    }
  })
}

/** Try to load visibility keys from the backend.
 *
 *  Returns:
 *   - ``{ kind: 'customized', keys }`` when the profile has persisted a
 *     selection (including an explicit empty selection — "hide everything"),
 *   - ``{ kind: 'unset' }`` when the profile has never customized (curated
 *     defaults apply),
 *   - ``null`` when the backend is unreachable (caller should fall back).
 */
type BackendVisibility =
  | { kind: 'customized'; keys: Set<string> }
  | { kind: 'unset' }

async function loadVisibleFromBackend(): Promise<BackendVisibility | null> {
  const gateway = $gateway.get()

  if (!gateway) {return null}

  try {
    const result = await gateway.request<{ keys: string[]; customized?: boolean }>(
      'model_visibility.get',
      { profile: visibilityProfile() }
    )

    if (result && Array.isArray(result.keys)) {
      const keys = new Set(result.keys.filter((x): x is string => typeof x === 'string'))

      // A missing file is reported as `customized: false`; treat that as unset
      // so an empty array is NOT conflated with the user hiding everything.
      return result.customized === false ? { kind: 'unset' } : { kind: 'customized', keys }
    }

    return null
  } catch {
    return null
  }
}

/** Migrate existing localStorage data to the backend. Returns the migrated
 *  set, or null if there was nothing to migrate. */
function migrateFromLocalStorage(): Set<string> | null {
  const raw = storedString(STORAGE_KEY)

  if (!raw) {return null}

  try {
    const parsed = JSON.parse(raw)

    return Array.isArray(parsed) ? new Set(parsed.filter((x): x is string => typeof x === 'string')) : null
  } catch {
    return null
  }
}

/** Load visibility preferences for the *given* profile and store them in the
 *  atom. Guards against a stale response overwriting a newer profile's state:
 *  the profile is snapshotted before the await, and the write is dropped if the
 *  active profile changed in the meantime. */
export async function syncVisibleModelsForProfile(profile: string): Promise<void> {
  const backend = await loadVisibleFromBackend()

  if (backend !== null) {
    // A profile switch landed while we were in flight — don't clobber the new
    // profile's freshly-loaded (or user-edited) state.
    if (normalizeProfileKey($activeGatewayProfile.get()) !== normalizeProfileKey(profile)) {
      return
    }

    if (backend.kind === 'customized') {
      $visibleModels.set(backend.keys)
      persistString(STORAGE_KEY, JSON.stringify([...backend.keys]))
    } else {
      // Never customized for this profile → curated defaults apply.
      $visibleModels.set(null)
      persistString(STORAGE_KEY, '')
    }

    return
  }

  const local = migrateFromLocalStorage()

  if (local !== null) {
    // Migrate localStorage data to backend (fire-and-forget, serialized)
    enqueueBackendSave(local)
    $visibleModels.set(local)

    return
  }

  // Backend unreachable and nothing local — keep the null sentinel.
}

/** Call once after gateway boot to sync visibility preferences for the active
 *  profile, and re-sync whenever the active gateway profile switches so one
 *  profile's preferences never bleed into another. Returns an unsubscribe fn. */
export function watchVisibleModels(): () => void {
  void syncVisibleModelsForProfile($activeGatewayProfile.get())

  return $activeGatewayProfile.subscribe(profile => {
    void syncVisibleModelsForProfile(profile)
  })
}

function loadVisible(): Set<string> | null {
  const raw = storedString(STORAGE_KEY)

  if (!raw) {
    return null
  }

  try {
    const parsed = JSON.parse(raw)

    return Array.isArray(parsed) ? new Set(parsed.filter((x): x is string => typeof x === 'string')) : null
  } catch {
    return null
  }
}

/** Explicit set of visible `provider::model` keys, or null when the user
 *  hasn't customized — in which case the curated default applies. */
export const $visibleModels = atom<Set<string> | null>(loadVisible())

export const $modelVisibilityOpen = atom(false)

export function setVisibleModels(keys: Set<string>): void {
  $visibleModels.set(new Set(keys))
  persistString(STORAGE_KEY, JSON.stringify([...keys]))
  // Serialized backend persist so a rapid toggle can't persist out of order.
  enqueueBackendSave(keys)
}

export function setModelVisibilityOpen(open: boolean): void {
  $modelVisibilityOpen.set(open)
}

/** The default-visible key set: the curated top-N per provider. Used both as
 *  the dropdown fallback and to seed the Edit Models dialog. */
export function defaultVisibleKeys(providers: readonly ModelOptionProvider[]): Set<string> {
  const keys = new Set<string>()

  for (const provider of providers) {
    expandProviderDefaults(provider, keys)
  }

  return keys
}

/** Add a provider's curated default model keys to `target`. Prefers the
 *  backend's `featured_models` shortlist (one flagship per lab) for aggregator
 *  providers that would otherwise flood the default view with dozens of models;
 *  falls back to the top-N collapsed families when a provider ships no featured
 *  list. Shared by `defaultVisibleKeys` and `resolveVisibleKeys` so the
 *  expansion rule lives in exactly one place. */
function expandProviderDefaults(provider: ModelOptionProvider, target: Set<string>): void {
  const families = collapseModelFamilies(provider.models ?? [])

  const featured = provider.featured_models ?? []

  const defaults = featured.length
    ? families.filter(family => featured.includes(family.id))
    : families.slice(0, DEFAULT_VISIBLE_PER_PROVIDER)

  for (const family of defaults) {
    target.add(modelVisibilityKey(provider.slug, family.id))
  }
}

/** Resolve the canonical working set: the user's stored keys plus the curated
 *  default expansion for any provider they haven't customized. Hide-all
 *  sentinels are PRESERVED here — this is the set the toggle handler mutates and
 *  persists, so dropping a sentinel would silently re-enable a provider the user
 *  emptied. Use `effectiveVisibleKeys` for display (sentinels stripped). */
export function resolveVisibleKeys(stored: Set<string> | null, providers: readonly ModelOptionProvider[]): Set<string> {
  if (!stored) {
    return defaultVisibleKeys(providers)
  }

  if (stored.size === 0) {
    return new Set()
  }

  const next = new Set(stored)

  for (const provider of providers) {
    const providerPrefix = `${provider.slug}::`

    const hasStoredProvider = [...stored].some(key => key.startsWith(providerPrefix) && !isProviderSentinel(key))

    const hasSentinel = stored.has(emptyProviderSentinelKey(provider.slug))

    if (hasStoredProvider || hasSentinel) {
      continue
    }

    expandProviderDefaults(provider, next)
  }

  return next
}

/** Resolve which keys are currently visible for DISPLAY: the resolved working
 *  set with bookkeeping sentinels stripped (they are not real models). */
export function effectiveVisibleKeys(
  stored: Set<string> | null,
  providers: readonly ModelOptionProvider[]
): Set<string> {
  const next = resolveVisibleKeys(stored, providers)

  // Strip sentinel keys — they are bookkeeping, not real visibility entries.
  for (const key of [...next]) {
    if (isProviderSentinel(key)) {
      next.delete(key)
    }
  }

  return next
}

/** Compute the next persisted visibility set when one model row is toggled.
 *  Seeds from `resolveVisibleKeys` (NOT `effectiveVisibleKeys`) so other
 *  providers' hide-all sentinels survive the persist. When the last visible
 *  model of a provider is toggled off, a sentinel records the explicit
 *  hide-all; re-enabling a model clears THAT provider's sentinel (only). */
export function toggleModelVisibility(
  stored: Set<string> | null,
  providers: readonly ModelOptionProvider[],
  providerSlug: string,
  model: string
): Set<string> {
  // `resolveVisibleKeys` always returns a fresh Set, so we can mutate it directly.
  const next = resolveVisibleKeys(stored, providers)
  const key = modelVisibilityKey(providerSlug, model)
  const sentinel = emptyProviderSentinelKey(providerSlug)

  if (next.has(key)) {
    next.delete(key)

    // Check if this was the last real model for this provider.
    const remainingForProvider = [...next].some(k => k.startsWith(`${providerSlug}::`) && !isProviderSentinel(k))

    if (!remainingForProvider) {
      next.add(sentinel)
    }
  } else {
    // Re-enabling promotes a previously hidden-all provider to an explicit
    // set of exactly the one re-enabled model — the curated defaults are NOT
    // restored. Intentional: "you hid everything, you get back only what you
    // re-enable." (Locked in by the sentinel-clear-on-re-enable test.)
    next.delete(sentinel)
    next.add(key)
  }

  return next
}

/** Compute the next persisted visibility set when a provider's master switch is
 *  flipped. `visible=true` enables every one of the provider's collapsed model
 *  families (and clears its hide-all sentinel); `visible=false` removes them all
 *  and records the sentinel so the defaults are not silently re-expanded.
 *  Seeds from `resolveVisibleKeys` so other providers' state (including their
 *  sentinels) survives the persist, mirroring `toggleModelVisibility`. */
export function setProviderVisibility(
  stored: Set<string> | null,
  providers: readonly ModelOptionProvider[],
  providerSlug: string,
  visible: boolean
): Set<string> {
  const next = resolveVisibleKeys(stored, providers)
  const sentinel = emptyProviderSentinelKey(providerSlug)
  const provider = providers.find(p => p.slug === providerSlug)
  const families = collapseModelFamilies(provider?.models ?? [])

  // Drop every existing entry for this provider (real keys + sentinel); we
  // rebuild its state from scratch below.
  for (const key of [...next]) {
    if (key.startsWith(`${providerSlug}::`)) {
      next.delete(key)
    }
  }

  if (visible) {
    for (const family of families) {
      next.add(modelVisibilityKey(providerSlug, family.id))
    }

    // A provider with zero models can't be "all on" — leave it empty rather
    // than stranding a sentinel that reads as an explicit hide-all.
    if (families.length === 0) {
      next.delete(sentinel)
    }
  } else {
    next.add(sentinel)
  }

  return next
}
