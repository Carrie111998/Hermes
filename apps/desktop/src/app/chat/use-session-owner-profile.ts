import { useStore } from '@nanostores/react'

import { useStoreSelector } from '@/lib/use-session-slice'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $sessions, rememberedSessionProfile } from '@/store/session'

import { useSessionView } from './session-view'

/**
 * The profile that owns the chat surface currently rendering this component.
 *
 * Session tiles can remain mounted while another profile is active, so the
 * global gateway profile is only a fallback for fresh sessions that do not yet
 * have a stored row. The stored session's stamped owner is authoritative.
 */
export function useSessionOwnerProfile(): string {
  const storedSessionId = useStore(useSessionView().$storedId)
  const activeProfile = useStore($activeGatewayProfile)

  return useStoreSelector($sessions, sessions =>
    normalizeProfileKey(rememberedSessionProfile(sessions, storedSessionId, activeProfile))
  )
}
