import { atom } from 'nanostores'

import { createSessionSpace, listSessionSpaces, setSessionSpace } from '@/hermes'
import type { SessionSpace } from '@/hermes'

import { setSessions } from './session'

export const $sessionSpaces = atom<SessionSpace[]>([])

export async function refreshSessionSpaces(profile?: string | null): Promise<void> {
  const result = await listSessionSpaces(profile)

  $sessionSpaces.set(result.spaces)
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

  $sessionSpaces.set([...$sessionSpaces.get(), space].sort((a, b) => a.name.localeCompare(b.name)))
  await assignSessionSpace(sessionId, space.id, profile)

  return space
}
