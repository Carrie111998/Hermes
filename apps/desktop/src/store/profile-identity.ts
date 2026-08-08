/**
 * Lightweight profile identity atoms — no gateway/projects imports.
 *
 * Session/project modules can read the active / new-chat profile without
 * pulling in the full profile switcher (which depends on projects cleanup).
 */
import { atom } from 'nanostores'

// Canonical key for a profile: trimmed, empty → "default".
export function normalizeProfileKey(name: string | null | undefined): string {
  const value = (name ?? '').trim()

  return value || 'default'
}

// The profile the live gateway WebSocket is currently connected to.
export const $activeGatewayProfile = atom<string>('default')

// Profile for the NEXT new chat (chosen via the new-chat picker). null = use
// the live gateway profile.
export const $newChatProfile = atom<string | null>(null)
