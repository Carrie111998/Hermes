import { atom } from 'nanostores'

import {
  createSessionSpace,
  getApiRequestConnection,
  listSessionSpaces,
  profileScopeKey,
  setSessionSpace
} from '@/hermes'
import type { SessionSpace } from '@/hermes'

import { setSessions } from './session'

export const $sessionSpacesByScope = atom<Record<string, SessionSpace[]>>({})

const refreshRevisions = new Map<string, number>()

export function sessionSpacesScopeKey(profile?: string | null, connectionId = getApiRequestConnection()): string {
  return profileScopeKey({ connectionId, profile })
}

export function sessionSpacesForScope(profile?: string | null, connectionId = getApiRequestConnection()): SessionSpace[] {
  return $sessionSpacesByScope.get()[sessionSpacesScopeKey(profile, connectionId)] ?? []
}

export async function refreshSessionSpaces(profile?: string | null): Promise<void> {
  const key = sessionSpacesScopeKey(profile)
  const revision = (refreshRevisions.get(key) ?? 0) + 1

  refreshRevisions.set(key, revision)
  const result = await listSessionSpaces(profile)

  if (refreshRevisions.get(key) !== revision) {
    return
  }

  $sessionSpacesByScope.set({ ...$sessionSpacesByScope.get(), [key]: result.spaces })
}

export async function assignSessionSpace(sessionId: string, spaceId: null | string, profile?: string): Promise<void> {
  let previous: null | string | undefined

  setSessions(current =>
    current.map(session => {
      if (session.id !== sessionId) {
        return session
      }

      previous = session.space_id

      return { ...session, space_id: spaceId }
    })
  )

  try {
    await setSessionSpace(sessionId, spaceId, profile)
  } catch (error) {
    setSessions(current =>
      current.map(session => (session.id === sessionId ? { ...session, space_id: previous ?? null } : session))
    )
    throw error
  }
}

export async function createAndAssignSessionSpace(
  sessionId: string,
  name: string,
  profile?: string
): Promise<SessionSpace> {
  const { space } = await createSessionSpace({ name }, profile)
  const key = sessionSpacesScopeKey(profile)
  const current = $sessionSpacesByScope.get()[key] ?? []

  $sessionSpacesByScope.set({
    ...$sessionSpacesByScope.get(),
    [key]: [...current, space].sort((a, b) => a.name.localeCompare(b.name))
  })
  await assignSessionSpace(sessionId, space.id, profile)

  return space
}
