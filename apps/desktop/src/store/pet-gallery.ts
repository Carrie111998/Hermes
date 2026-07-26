import { atom, computed } from 'nanostores'

import { normalize } from '@/lib/text'
import { $petInfo, type PetInfo, petProfile, setPetInfo } from '@/store/pet'
import { setProfilePetInfo } from '@/store/pet-multi'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { petRequestFor, type PetGatewayRequest } from '@/store/pet-transport'

/**
 * Feature store for the petdex gallery picker (Cmd+K "Pets…" + Settings).
 *
 * Why this exists: `pet.gallery` does a *network* manifest fetch on the gateway,
 * so re-pulling it after every adopt/toggle made the picker feel laggy and made
 * two components (palette + settings) each carry their own copy of the same
 * fetch / thumb-cache / optimistic-mutation logic. This store centralizes it:
 *
 *  - The gallery is fetched once and cached; reopening the picker is instant.
 *  - Mutations (adopt / enable / remove) patch local state and only re-pull the
 *    cheap, local `pet.info` — never the network manifest again.
 *  - Thumbnails are deduped in a process-global cache (the backend disk-caches
 *    too, so a slug is fetched at most once per session).
 *
 * Layer 7: state is per-profile (one gallery slice per normalized profile key)
 * so pinned mode can manage several profiles' pets concurrently. The legacy
 * singular atoms ($petGallery et al) remain as computed views over the ACTIVE
 * profile's slice, so every existing consumer (follow-active) is unchanged. A
 * non-active profile's calls route through petRequestFor(profile) — never the
 * active gateway; the active profile keeps the hook's recovering requester.
 */

export interface GalleryPet {
  slug: string
  displayName: string
  installed: boolean
  spritesheetUrl?: string
  /** petdex's hand-picked set — used only to rank "popular" pets first. */
  curated?: boolean
  /** Hatched locally by the user (createdBy=generator) — badged + ranked first. */
  generated?: boolean
}

export interface PetGallery {
  enabled: boolean
  active: string
  pets: GalleryPet[]
}

export type PetGalleryStatus = 'idle' | 'loading' | 'ready' | 'stale' | 'error'

/** The recovering `requestGateway` from `useGatewayRequest` — passed in so the
 *  store reuses the hook's reconnect/reauth handling instead of duplicating it. */
export type GatewayRequest = PetGatewayRequest

/** A JSON-RPC "method not found" — the backend predates the pet RPCs. */
function isMissingMethod(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /method not found|-32601|unknown method|no such method/i.test(message)
}

// ── Per-profile state (Layer 7) ────────────────────────────────────────────
// One slice per normalized profile key. Copy-on-write Maps so computeds re-fire.

export const $petGalleries = atom<ReadonlyMap<string, PetGallery>>(new Map())
export const $petGalleryStatuses = atom<ReadonlyMap<string, PetGalleryStatus>>(new Map())
export const $petGalleryErrors = atom<ReadonlyMap<string, string | null>>(new Map())
export const $petBusyProfiles = atom<ReadonlyMap<string, string | null>>(new Map())

function activeKey(): string {
  return normalizeProfileKey($activeGatewayProfile.get())
}

function slice<T>(map: ReadonlyMap<string, T>, key: string): T | undefined {
  return map.get(key)
}

// Backwards-compat singular views over the ACTIVE profile's slice. Existing
// consumers (follow-active) read these and see exactly what they did before.
export const $petGallery = computed($petGalleries, m => slice(m, activeKey()) ?? null)
export const $petGalleryStatus = computed($petGalleryStatuses, m => slice(m, activeKey()) ?? 'idle')
export const $petGalleryError = computed($petGalleryErrors, m => slice(m, activeKey()) ?? null)
export const $petBusy = computed($petBusyProfiles, m => slice(m, activeKey()) ?? null)

// Which action is in flight, so rows/buttons can show a spinner. A slug for a
// per-pet mutation; the `TOGGLE_*` sentinels for the on/off switch.
export const TOGGLE_ON = '\u0000on'
export const TOGGLE_OFF = '\u0000off'

// Process-global caches (survive component unmount → instant reopen). Thumb cache
// is keyed `${profile}::${slug}` so two profiles' same-named pets don't collide.
const thumbCache = new Map<string, Promise<string | null>>()
// One in-flight gallery load per profile — concurrent profile loads must not
// cancel each other (test 27).
const galleryLoads = new Map<string, Promise<void>>()
// One scale-persist debounce timer per profile (test 47).
const scalePersists = new Map<string, ReturnType<typeof setTimeout>>()

function thumbKey(profile: string, slug: string): string {
  return `${normalizeProfileKey(profile)}::${slug}`
}

function setGallerySlice(profile: string, gallery: PetGallery | null): void {
  const key = normalizeProfileKey(profile)
  const next = new Map($petGalleries.get())

  if (gallery) {
    next.set(key, gallery)
  } else {
    next.delete(key)
  }

  $petGalleries.set(next)
}

function setStatusSlice(profile: string, status: PetGalleryStatus): void {
  const key = normalizeProfileKey(profile)
  const next = new Map($petGalleryStatuses.get())
  next.set(key, status)
  $petGalleryStatuses.set(next)
}

function setErrorSlice(profile: string, error: string | null): void {
  const key = normalizeProfileKey(profile)
  const next = new Map($petGalleryErrors.get())
  next.set(key, error)
  $petGalleryErrors.set(next)
}

function setBusySlice(profile: string, busy: string | null): void {
  const key = normalizeProfileKey(profile)
  const next = new Map($petBusyProfiles.get())
  next.set(key, busy)
  $petBusyProfiles.set(next)
}

/** Pick the requester for a profile: the active profile keeps the hook's
 *  recovering requester; any other profile routes through its OWN socket via
 *  petRequestFor (never the active gateway). */
function requestFor(profile: string, activeRequest: GatewayRequest): GatewayRequest {
  return normalizeProfileKey(profile) === activeKey() ? activeRequest : petRequestFor(profile)
}

/** Profile-scoped pet RPC. Pets are per-profile, so every call carries the
 *  profile (the gateway no-ops it for the launch profile). */
function petRpc<T>(
  request: GatewayRequest,
  profile: string,
  method: string,
  params: Record<string, unknown> = {}
): Promise<T> {
  const key = normalizeProfileKey(profile)

  return request<T>(method, { ...params, profile: key })
}

/** Write a profile's mascot info: the active profile mirrors into the global
 *  `$petInfo` (the floating pet + overlay render from it); a background profile
 *  updates only its own `$profilePets` slice. */
function applyPetInfo(profile: string, info: PetInfo): void {
  if (normalizeProfileKey(profile) === activeKey()) {
    setPetInfo(info)
  } else {
    setProfilePetInfo(profile, info)
  }
}

function currentInfo(profile: string): PetInfo {
  return normalizeProfileKey(profile) === activeKey() ? $petInfo.get() : { enabled: false }
}

/**
 * Drop the cached gallery, thumbnails, and in-flight loads so the next open
 * refetches. Called on a profile switch (pets are per-profile). Clears every
 * profile's slice — the picker reloads against the now-active backend.
 */
export function resetPetGallery(): void {
  galleryLoads.clear()
  thumbCache.clear()
  $petGalleries.set(new Map())
  $petGalleryStatuses.set(new Map())
  $petGalleryErrors.set(new Map())
  $petBusyProfiles.set(new Map())
}

export function loadPetThumb(
  request: GatewayRequest,
  slug: string,
  url?: string,
  profile: string = petProfile()
): Promise<string | null> {
  const key = thumbKey(profile, slug)
  let pending = thumbCache.get(key)

  if (!pending) {
    const rpc = requestFor(profile, request)
    pending = petRpc<{ ok: boolean; dataUri?: string }>(rpc, profile, 'pet.thumb', { slug, url: url ?? '' })
      .then(result => (result?.ok && result.dataUri ? result.dataUri : null))
      .catch(() => null)
    thumbCache.set(key, pending)
  }

  return pending
}

/**
 * Fetch one profile's gallery once and cache it. Subsequent calls are no-ops
 * while a ready snapshot is held; pass `{ force: true }` to bypass the cache.
 * Concurrent callers for the SAME profile share a single in-flight request;
 * different profiles load independently (never cancel each other).
 */
export function loadPetGallery(
  request: GatewayRequest,
  options: { force?: boolean; profile?: string } = {}
): Promise<void> {
  const profile = normalizeProfileKey(options.profile ?? petProfile())
  const rpc = requestFor(profile, request)

  if (!options.force && slice($petGalleries.get(), profile) && $petGalleryStatuses.get().get(profile) === 'ready') {
    return Promise.resolve()
  }

  const existing = galleryLoads.get(profile)

  if (existing) {
    return existing
  }

  const load = (async () => {
    if (!slice($petGalleries.get(), profile)) {
      setStatusSlice(profile, 'loading')
    }

    let localOk = false

    try {
      // Phase 1: local pets only — instant, never blocks on the remote petdex
      // manifest. The user's own/generated pets render right away.
      const [local, info] = await Promise.all([
        petRpc<PetGallery>(rpc, profile, 'pet.gallery', { localOnly: true }),
        petRpc<PetInfo>(rpc, profile, 'pet.info')
      ])

      if (local) {
        setGallerySlice(profile, local)
        setStatusSlice(profile, 'ready')
        setErrorSlice(profile, null)
        localOk = true
      }

      if (info) {
        applyPetInfo(profile, info)
      }
    } catch (e) {
      if (isMissingMethod(e)) {
        setStatusSlice(profile, 'stale')
      } else if (!slice($petGalleries.get(), profile)) {
        // Only surface a hard error when we have nothing to show; a transient
        // hiccup mid-session leaves the cached gallery intact.
        setStatusSlice(profile, 'error')
        setErrorSlice(profile, e instanceof Error ? e.message : 'Could not reach the petdex gallery.')
      }
    } finally {
      galleryLoads.delete(profile)
    }

    // Phase 2: merge in the full petdex catalog in the background. A slow/failed
    // manifest fetch never hides the local pets shown in phase 1.
    if (localOk) {
      try {
        const full = await petRpc<PetGallery>(rpc, profile, 'pet.gallery')

        if (full) {
          setGallerySlice(profile, full)
          setStatusSlice(profile, 'ready')
        }
      } catch {
        // Keep the local-only gallery; the petdex catalog just stays unmerged.
      }
    }
  })()

  galleryLoads.set(profile, load)

  return load
}

// Push the live mascot state (cheap, local config read) without re-pulling the
// network gallery — the floating pet repaints, the picker keeps its cache.
async function syncInfo(request: GatewayRequest, profile: string): Promise<void> {
  try {
    const rpc = requestFor(profile, request)
    const info = await petRpc<PetInfo>(rpc, profile, 'pet.info')

    if (info) {
      applyPetInfo(profile, info)
    }
  } catch {
    // The mutation already succeeded; a stale mascot self-heals on its poll.
  }
}

/**
 * Reflect a just-adopted *local* pet without any network: optimistically mark it
 * active/installed in the cached gallery and repaint the live mascot via the
 * local `pet.info`. Adopting a generated pet is a disk+config op — it must never
 * wait on `pet.gallery`'s remote petdex manifest fetch.
 */
export async function applyAdoptedPet(
  request: GatewayRequest,
  slug: string,
  displayName: string,
  profile: string = petProfile()
): Promise<void> {
  patchGallery(profile, gallery => ({
    ...gallery,
    enabled: true,
    active: slug,
    pets: gallery.pets.some(p => p.slug === slug)
      ? gallery.pets.map(p => (p.slug === slug ? { ...p, installed: true, displayName } : p))
      : [...gallery.pets, { slug, displayName, installed: true, spritesheetUrl: '' }]
  }))
  await syncInfo(request, profile)
}

/**
 * Filter (drop the internal `clawd*` pets + apply a search query) and rank the
 * gallery for a picker. Ranking has no popularity data, so it leans on the
 * signals we do have: active pet first, then installed, then curated. Shared by
 * the Cmd-K palette and the Settings grid so the two can't drift — each caller
 * applies its own cap and reads `.length` for the total.
 */
export function rankedGalleryPets(gallery: PetGallery | null, query = ''): GalleryPet[] {
  if (!gallery) {
    return []
  }

  const needle = normalize(query)

  // User-generated pets first, then the active pet, then installed, then curated.
  // Guard every term with a boolean — local-only pets omit curated/generated, and
  // `Number(undefined)` is NaN, which poisons the sort (it would sink those pets
  // below the render cap and hide them entirely).
  const rank = (p: GalleryPet) =>
    (p.generated ? 8 : 0) +
    (gallery.enabled && p.slug === gallery.active ? 4 : 0) +
    (p.installed ? 2 : 0) +
    (p.curated ? 1 : 0)

  return gallery.pets
    .filter(
      p =>
        !/^clawd(-|$)/i.test(p.slug) &&
        (!needle || p.slug.toLowerCase().includes(needle) || p.displayName.toLowerCase().includes(needle))
    )
    .sort((a, b) => rank(b) - rank(a))
}

function patchGallery(profile: string, fn: (gallery: PetGallery) => PetGallery): void {
  const key = normalizeProfileKey(profile)
  const current = $petGalleries.get().get(key)

  if (current) {
    setGallerySlice(key, fn(current))
  }
}

/** Shared mutation wrapper: spin, fire, patch on success, surface failures. */
async function mutate(
  profile: string,
  busyKey: string,
  fallback: string,
  request: GatewayRequest,
  run: () => Promise<void>
): Promise<boolean> {
  setBusySlice(profile, busyKey)
  setErrorSlice(profile, null)

  try {
    await run()
    await syncInfo(request, profile)

    return true
  } catch (e) {
    if (isMissingMethod(e)) {
      setStatusSlice(profile, 'stale')
    } else {
      setErrorSlice(profile, e instanceof Error ? e.message : fallback)
    }

    return false
  } finally {
    setBusySlice(profile, null)
  }
}

/** Install (if needed) + activate a pet. Optimistically marks it active. */
export function adoptPet(
  request: GatewayRequest,
  slug: string,
  fallback: string,
  profile: string = petProfile()
): Promise<boolean> {
  return mutate(profile, slug, fallback, request, async () => {
    const rpc = requestFor(profile, request)
    await petRpc(rpc, profile, 'pet.select', { slug })
    patchGallery(profile, g => ({
      ...g,
      enabled: true,
      active: slug,
      pets: g.pets.map(p => (p.slug === slug ? { ...p, installed: true } : p))
    }))
  })
}

/**
 * Turn the floating mascot on/off. On enable, activates the current pet (or the
 * first installed one). Returns false without firing if there's nothing to show.
 */
export function setPetEnabled(
  request: GatewayRequest,
  on: boolean,
  copy: { noneAvailable: string; fallback: string },
  profile: string = petProfile()
): Promise<boolean> {
  const gallery = $petGalleries.get().get(normalizeProfileKey(profile))

  if (!on && !(gallery?.enabled ?? false)) {
    return Promise.resolve(true)
  }

  let slug = gallery?.active || ''

  if (on) {
    slug = slug || gallery?.pets.find(p => p.installed)?.slug || ''

    if (!slug) {
      setErrorSlice(profile, copy.noneAvailable)

      return Promise.resolve(false)
    }
  }

  return mutate(profile, on ? TOGGLE_ON : TOGGLE_OFF, copy.fallback, request, async () => {
    const rpc = requestFor(profile, request)

    if (on) {
      await petRpc(rpc, profile, 'pet.select', { slug })
    } else {
      await petRpc(rpc, profile, 'pet.disable')
    }

    patchGallery(profile, g => ({ ...g, enabled: on, active: on ? slug : g.active }))
  })
}

// Pet scale bounds — mirror `agent/pet/constants.py` (MIN_SCALE / MAX_SCALE) so
// the slider and the server clamp to the same range.
export const PET_SCALE_MIN = 0.1
export const PET_SCALE_MAX = 3.0
export const PET_SCALE_DEFAULT = 0.33
export const clampPetScale = (n: number) => Math.max(PET_SCALE_MIN, Math.min(PET_SCALE_MAX, n))

// Wheel → scale. Multiplicative so one notch feels the same at any size. Tuned
// for a discrete mouse-wheel notch (deltaY ≈ ±100); trackpad two-finger scroll
// (smaller deltas) just resizes more gently, which is fine.
const WHEEL_SCALE_K = 0.0015

/**
 * Next pet scale for one mouse-wheel step over the pet. Scrolling up (deltaY < 0)
 * grows it, scrolling down shrinks it; the result is clamped to the slider's range.
 */
export function nextScaleFromWheel(current: number | undefined, deltaY: number): number {
  const base = current ?? PET_SCALE_DEFAULT

  return clampPetScale(base * Math.exp(-deltaY * WHEEL_SCALE_K))
}

/**
 * Resize the floating pet. Updates the mascot info synchronously so the on-screen
 * pet (and the slider) react on the same frame, then debounce-persists to
 * `display.pet.scale` (per profile) so a slider drag fires one RPC, not one per
 * pixel. The active profile mirrors into `$petInfo`; a background profile updates
 * only its `$profilePets` slice.
 */
export function setPetScale(request: GatewayRequest, scale: number, profile: string = petProfile()): void {
  const key = normalizeProfileKey(profile)
  const next = clampPetScale(scale)

  applyPetInfo(key, { ...currentInfo(key), scale: next })

  const existing = scalePersists.get(key)

  if (existing) {
    clearTimeout(existing)
  }

  const rpc = requestFor(key, request)

  scalePersists.set(
    key,
    setTimeout(() => {
      scalePersists.delete(key)
      petRpc<{ ok: boolean; scale?: number }>(rpc, key, 'pet.scale', { scale: next })
        .then(result => {
          // Reconcile with the server's clamp (cheap; only matters at the bounds).
          if (typeof result?.scale === 'number' && result.scale !== currentInfo(key).scale) {
            applyPetInfo(key, { ...currentInfo(key), scale: result.scale })
          }
        })
        .catch(() => {
          // Cosmetic — the pet already resized; persistence self-heals next write.
        })
    }, 200)
  )
}

/** Export a pet as a `.zip` (pet.json + spritesheet) and save it via the browser. */
export async function exportPet(
  request: GatewayRequest,
  slug: string,
  fallback: string,
  profile: string = petProfile()
): Promise<boolean> {
  setBusySlice(profile, slug)
  setErrorSlice(profile, null)

  try {
    const rpc = requestFor(profile, request)
    const res = await petRpc<{ ok: boolean; filename: string; zipBase64: string }>(rpc, profile, 'pet.export', { slug })

    if (!res?.ok || !res.zipBase64) {
      throw new Error(fallback)
    }

    const bytes = Uint8Array.from(atob(res.zipBase64), c => c.charCodeAt(0))
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = res.filename || `${slug}.zip`
    anchor.click()
    URL.revokeObjectURL(url)

    return true
  } catch (e) {
    setErrorSlice(profile, e instanceof Error ? e.message : fallback)

    return false
  } finally {
    setBusySlice(profile, null)
  }
}

/**
 * Rename a pet — optimistic. The new name shows instantly (so the dialog can
 * close immediately); the RPC runs in the background and the backend also
 * realigns the slug/dir, so we reconcile the slug + thumb cache when it returns,
 * and roll the name back if it fails.
 */
export function renamePet(
  request: GatewayRequest,
  slug: string,
  name: string,
  fallback: string,
  profile: string = petProfile()
): Promise<boolean> {
  const trimmed = name.trim()

  if (!trimmed) {
    return Promise.resolve(false)
  }

  const key = normalizeProfileKey(profile)
  const prev = $petGalleries.get().get(key)?.pets.find(p => p.slug === slug)?.displayName ?? ''

  // Optimistic: paint the new name now (slug reconciles when the RPC returns).
  patchGallery(key, g => ({
    ...g,
    pets: g.pets.map(p => (p.slug === slug ? { ...p, displayName: trimmed } : p))
  }))
  setErrorSlice(key, null)

  return (async () => {
    try {
      const rpc = requestFor(key, request)
      const res = await petRpc<{ ok: boolean; slug: string; displayName: string }>(rpc, key, 'pet.rename', {
        slug,
        name: trimmed
      })

      if (!res?.ok) {
        throw new Error(fallback)
      }

      const newSlug = res.slug || slug

      if (newSlug !== slug) {
        thumbCache.delete(thumbKey(key, slug))
        patchGallery(key, g => ({
          ...g,
          active: g.active === slug ? newSlug : g.active,
          pets: g.pets
            .filter(p => p.slug !== newSlug || p.slug === slug)
            .map(p => (p.slug === slug ? { ...p, slug: newSlug, displayName: res.displayName || trimmed } : p))
        }))
      }

      return true
    } catch (e) {
      // Roll the optimistic name back so the list reflects on-disk truth.
      patchGallery(key, g => ({
        ...g,
        pets: g.pets.map(p => (p.slug === slug ? { ...p, displayName: prev } : p))
      }))
      setErrorSlice(key, e instanceof Error ? e.message : fallback)

      return false
    }
  })()
}

/** Uninstall a pet; turns the mascot off if it was the active one. */
export function removePet(
  request: GatewayRequest,
  slug: string,
  fallback: string,
  profile: string = petProfile()
): Promise<boolean> {
  return mutate(profile, slug, fallback, request, async () => {
    const rpc = requestFor(profile, request)
    await petRpc(rpc, profile, 'pet.remove', { slug })
    // Evict the by-slug thumb cache so a reused slug doesn't render this pet's
    // stale thumbnail (the backend drops its disk thumb in parallel).
    thumbCache.delete(thumbKey(profile, slug))
    patchGallery(profile, g => ({
      ...g,
      enabled: g.active === slug ? false : g.enabled,
      active: g.active === slug ? '' : g.active,
      // Petdex pets can be reinstalled from the manifest, so we just mark them
      // uninstalled. Generated / local-only pets have no remote source — once
      // deleted they're gone, so drop them from the list entirely.
      pets: g.pets.flatMap(p => {
        if (p.slug !== slug) {
          return [p]
        }

        return p.generated || !p.spritesheetUrl ? [] : [{ ...p, installed: false }]
      })
    }))
  })
}
