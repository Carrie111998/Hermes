import { storedString, storedStringRecord } from '@/lib/storage'

// Legacy global skin (and the default profile's slot).
export const SKIN_STORAGE_KEY = 'hermes-desktop-theme-v2'
// Per-profile skin assignments. Named profiles inherit the global slot until
// they receive their own explicit appearance.
export const PROFILE_SKINS_STORAGE_KEY = 'hermes-desktop-profile-themes-v1'

/**
 * Whether Desktop already owns a persisted skin choice for this profile.
 *
 * Check raw presence rather than theme resolution: a backend-authored skin is
 * not registered until `gateway.ready`, so resolving its stored name during
 * boot would incorrectly classify a real user choice as absent.
 */
export function hasStoredSkinPreference(profile: string): boolean {
  if (storedString(SKIN_STORAGE_KEY) !== null) {
    return true
  }

  return profile !== 'default' && Object.hasOwn(storedStringRecord(PROFILE_SKINS_STORAGE_KEY), profile)
}
