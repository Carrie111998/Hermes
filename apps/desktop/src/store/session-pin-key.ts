import type { SessionInfo } from '@/types/hermes'

import { sessionMatchesStoredId, sessionPinId } from './session'

const SESSION_PIN_KEY_SEPARATOR = '\0'

export interface ParsedSessionPinKey {
  id: string
  profile: string
}

export function normalizeSessionPinProfile(profile?: null | string): string {
  return profile?.trim() || 'default'
}

export function createSessionPinKey(profile: null | string | undefined, id: string): string {
  return `${normalizeSessionPinProfile(profile)}${SESSION_PIN_KEY_SEPARATOR}${id}`
}

export function parseSessionPinKey(key: string): null | ParsedSessionPinKey {
  const separator = key.indexOf(SESSION_PIN_KEY_SEPARATOR)

  if (separator <= 0 || separator === key.length - 1) {
    return null
  }

  return {
    id: key.slice(separator + 1),
    profile: normalizeSessionPinProfile(key.slice(0, separator))
  }
}

export function sessionPinKey(session: SessionInfo): string {
  return createSessionPinKey(session.profile, sessionPinId(session))
}

export function sessionScopedId(session: SessionInfo): string {
  return createSessionPinKey(session.profile, session.id)
}

export function sessionMatchesPinKey(session: SessionInfo, key: string): boolean {
  const parsed = parseSessionPinKey(key)

  if (!parsed) {
    return sessionMatchesStoredId(session, key)
  }

  return normalizeSessionPinProfile(session.profile) === parsed.profile && sessionMatchesStoredId(session, parsed.id)
}

export function sessionPinIdsForScope(keys: readonly string[], scope?: string): string[] {
  const normalizedScope = scope ? normalizeSessionPinProfile(scope) : null
  const ids: string[] = []

  for (const key of keys) {
    const parsed = parseSessionPinKey(key)

    if (!parsed) {
      ids.push(key)
    } else if (!normalizedScope || parsed.profile === normalizedScope) {
      ids.push(parsed.id)
    }
  }

  return ids
}
