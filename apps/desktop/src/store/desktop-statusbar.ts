import { atom } from 'nanostores'

import { getApiRequestProfile, getHermesConfigRecord, saveHermesConfig } from '@/hermes'
import { readKey, writeKey } from '@/lib/storage'
import { setHermesConfigCache } from '@/store/hermes-config-record'
import type { HermesConfigRecord } from '@/types/hermes'

export type DesktopStatusbarMode = 'auto-hide' | 'off' | 'on'

const LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY = 'hermes.desktop.statusbarVisible'

export const DEFAULT_DESKTOP_STATUSBAR_MODE: DesktopStatusbarMode = 'off'

function legacyDesktopStatusbarMode(): DesktopStatusbarMode | null {
  const value = readKey(LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY)

  return value === 'true' ? 'on' : value === 'false' ? 'off' : null
}

export const $desktopStatusbarMode = atom<DesktopStatusbarMode>(
  legacyDesktopStatusbarMode() ?? DEFAULT_DESKTOP_STATUSBAR_MODE
)

interface PendingStatusbarIntent {
  generation: number
  mode: DesktopStatusbarMode
}

const confirmedModes = new Map<string, DesktopStatusbarMode>()
const pendingIntents = new Map<string, PendingStatusbarIntent>()

let intentGeneration = 0
let legacyMigration: null | Promise<HermesConfigRecord | null> = null
let persistenceQueue: Promise<void> = Promise.resolve()

export function normalizeDesktopStatusbarMode(value: unknown): DesktopStatusbarMode {
  return value === 'off' || value === 'auto-hide' || value === 'on' ? value : DEFAULT_DESKTOP_STATUSBAR_MODE
}

function profileKey(profile: null | string): string {
  return profile || 'default'
}

function publishDesktopStatusbarMode(mode: DesktopStatusbarMode): void {
  $desktopStatusbarMode.set(mode)
}

export function applyDesktopStatusbarFromConfig(
  config: { display?: { desktop_statusbar?: unknown } | null } | null | undefined
): void {
  const key = profileKey(getApiRequestProfile())
  const configuredMode = normalizeDesktopStatusbarMode(config?.display?.desktop_statusbar)

  // v2026.7.30 persisted the old whole-bar boolean before this profile-scoped
  // setting existed. The new config default is off, so a legacy value only
  // needs to win while the effective config is still that default. A real
  // on/auto-hide config already expresses newer intent and takes precedence.
  const mode =
    configuredMode === DEFAULT_DESKTOP_STATUSBAR_MODE
      ? (legacyDesktopStatusbarMode() ?? configuredMode)
      : configuredMode

  confirmedModes.set(key, mode)
  publishDesktopStatusbarMode(pendingIntents.get(key)?.mode ?? mode)
}

/**
 * Persist the profile-scoped Desktop status bar preference. The atom updates
 * optimistically so the chrome responds immediately, then rolls back if the
 * whole-record config write fails.
 */
export function persistDesktopStatusbarMode(mode: DesktopStatusbarMode): Promise<HermesConfigRecord> {
  const profile = getApiRequestProfile()
  const key = profileKey(profile)
  const generation = ++intentGeneration
  const fallbackMode = confirmedModes.get(key) ?? $desktopStatusbarMode.get()

  pendingIntents.set(key, { generation, mode })
  publishDesktopStatusbarMode(mode)

  const operation = persistenceQueue.then(async () => {
    const record = await getHermesConfigRecord(profile)

    const display =
      record.display && typeof record.display === 'object' && !Array.isArray(record.display)
        ? (record.display as Record<string, unknown>)
        : {}

    const next = { ...record, display: { ...display, desktop_statusbar: mode } }

    await saveHermesConfig(next, profile)
    writeKey(LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY, null)
    confirmedModes.set(key, mode)

    if (pendingIntents.get(key)?.generation === generation) {
      pendingIntents.delete(key)

      if (profileKey(getApiRequestProfile()) === key) {
        publishDesktopStatusbarMode(mode)
      }
    }

    if (profileKey(getApiRequestProfile()) === key) {
      setHermesConfigCache(next)
    }

    return next
  })

  persistenceQueue = operation.then(
    () => undefined,
    () => undefined
  )

  return operation.catch(error => {
    if (pendingIntents.get(key)?.generation === generation) {
      pendingIntents.delete(key)

      if (profileKey(getApiRequestProfile()) === key) {
        publishDesktopStatusbarMode(confirmedModes.get(key) ?? fallbackMode)
      }
    }

    throw error
  })
}

/**
 * Move the pre-config whole-bar preference into the active profile once. The
 * old key is removed only after a successful config write, so an unavailable
 * backend leaves the user's current choice in place and can retry later.
 */
export function migrateLegacyDesktopStatusbarPreference(
  config: { display?: { desktop_statusbar?: unknown } | null } | null | undefined
): Promise<HermesConfigRecord | null> {
  if (legacyMigration) {
    return legacyMigration
  }

  const legacyMode = legacyDesktopStatusbarMode()

  if (!legacyMode) {
    return Promise.resolve(null)
  }

  const configuredMode = normalizeDesktopStatusbarMode(config?.display?.desktop_statusbar)

  if (configuredMode !== DEFAULT_DESKTOP_STATUSBAR_MODE) {
    writeKey(LEGACY_STATUSBAR_VISIBLE_STORAGE_KEY, null)

    return Promise.resolve(null)
  }

  legacyMigration = persistDesktopStatusbarMode(legacyMode).finally(() => {
    legacyMigration = null
  })

  return legacyMigration
}

/**
 * Shared whole-bar toggle used by the keybind, command palette, and context
 * menu. The whole-bar action remains binary like main's original control;
 * auto-hide stays available as an explicit Appearance setting.
 */
export async function toggleDesktopStatusbarVisible(): Promise<HermesConfigRecord> {
  const current = $desktopStatusbarMode.get()

  return persistDesktopStatusbarMode(current === 'off' ? 'on' : 'off')
}
