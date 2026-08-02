/**
 * Live skin sync from the Hermes backend.
 *
 * The backend resolves the active skin (built-in or `$HERMES_HOME/skins/*.yaml`)
 * and announces it on `gateway.ready` / `skin.changed`, and answers `config.get
 * skin` with the same payload. `ingestBackendSkin` folds that into the desktop:
 *
 *   1. Registers the converted theme in `$backendThemes` so it appears wherever a
 *      built-in does — Appearance, Cmd-K, `/skin` — with no per-surface wiring
 *      (`listAllThemes` merges this store).
 *   2. When asked to apply (a first-use backend choice or an explicit runtime
 *      change), queues the switch in `$pendingSkinApplies`, which the
 *      ThemeProvider drains through `setTheme`.
 *
 * `gateway.ready` adopts a concrete backend skin only when Desktop has no
 * persisted choice for the active profile. Otherwise it only seeds the
 * registry, so reconnects never stomp the user's Desktop preference.
 */

import type { HermesSkin } from '@hermes/shared/skin'
import { atom } from 'nanostores'

import { BUILTIN_THEMES } from './presets'
import { skinToDesktopTheme } from './skin'
import { hasStoredSkinPreference } from './skin-preference'
import type { DesktopTheme } from './types'

/** Skins pushed by the backend, keyed by name. Merged by `listAllThemes`. */
export const $backendThemes = atom<Record<string, DesktopTheme>>({})

export interface PendingSkinApply {
  name: string
  profile: string
}

/** Profile-scoped switches the ThemeProvider should drain. */
export const $pendingSkinApplies = atom<PendingSkinApply[]>([])

// Background gateways remain live, so both their theme registries and apply
// guards must remain profile-scoped. Only the active profile's themes are
// published through $backendThemes; the rest stay cached until activation.
const themesByProfile = new Map<string, Record<string, DesktopTheme>>()
const lastSyncedByProfile = new Map<string, { applied: boolean; name: string }>()
let activeThemeProfile = 'default'

const normalizeProfile = (profile: string | null | undefined): string => (profile ?? '').trim() || 'default'

export function activateBackendSkinProfile(profile: string): void {
  const key = normalizeProfile(profile)
  activeThemeProfile = key
  $backendThemes.set(themesByProfile.get(key) ?? {})
}

/** Test-only: reset the module's apply guard + registry between cases. */
export function __resetBackendSkinSync(): void {
  themesByProfile.clear()
  lastSyncedByProfile.clear()
  activeThemeProfile = 'default'
  $backendThemes.set({})
  $pendingSkinApplies.set([])
}

/**
 * Fold a resolved skin into the desktop. `apply: false` only records the
 * baseline; `apply: true` repaints on a name change. Built-in names keep the
 * desktop's own palette but can still be applied.
 */
export function ingestBackendSkin(
  skin: HermesSkin | undefined | null,
  { apply, profile = 'default' }: { apply: boolean; profile?: string }
): void {
  const name = (skin && typeof skin === 'object' ? (skin.name ?? '') : '').trim()
  const profileKey = normalizeProfile(profile)

  if (!name) {
    return
  }

  // `default` is "no opinion" on the PALETTE — the desktop keeps its own default
  // (nous), so we never register a converted theme under `default`. It is still a
  // valid apply TARGET though: a runtime switch back to `default` must repaint the
  // desktop to its own default (setTheme normalizes `default` → nous). So we only
  // skip the registry step here and let it flow through the apply logic below.
  // Built-in names (mono/slate/…) already have a hand-tuned desktop palette — we
  // never shadow it, but the name is still a valid apply target.
  if (name !== 'default' && !BUILTIN_THEMES[name]) {
    const theme = skinToDesktopTheme(skin as HermesSkin)

    if (!theme) {
      return
    }

    const current = themesByProfile.get(profileKey) ?? {}

    if (JSON.stringify(current[name]) !== JSON.stringify(theme)) {
      const next = { ...current, [name]: theme }
      themesByProfile.set(profileKey, next)

      if (profileKey === activeThemeProfile) {
        $backendThemes.set(next)
      }
    }
  }

  if (!apply) {
    // Connect-time seed: record without painting. A reconnect re-seed keeps an
    // earlier real apply's flag so repeat events can't override a manual switch.
    const lastSynced = lastSyncedByProfile.get(profileKey)

    if (lastSynced?.name !== name || !lastSynced.applied) {
      lastSyncedByProfile.set(profileKey, { applied: false, name })
    }

    return
  }

  const lastSynced = lastSyncedByProfile.get(profileKey)

  if (name !== lastSynced?.name || !lastSynced.applied) {
    lastSyncedByProfile.set(profileKey, { applied: true, name })

    // Keep the latest command for each profile without dropping commands from
    // other profiles whose gateway events arrived in the same React turn.
    const pending = $pendingSkinApplies.get().filter(item => item.profile !== profileKey)
    $pendingSkinApplies.set([...pending, { name, profile: profileKey }])
  }
}

/**
 * Register the active backend skin at connect time and adopt it only when
 * Desktop has no persisted appearance choice for the active profile.
 */
export function ingestGatewayReadySkin(skin: HermesSkin | undefined | null, profile: string): void {
  const name = (skin && typeof skin === 'object' ? (skin.name ?? '') : '').trim()

  ingestBackendSkin(skin, {
    apply: name !== 'default' && !hasStoredSkinPreference(profile),
    profile
  })
}
