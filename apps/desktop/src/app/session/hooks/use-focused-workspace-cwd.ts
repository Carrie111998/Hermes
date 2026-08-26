import { useStore } from '@nanostores/react'

import { useStoreSelector } from '@/lib/use-session-slice'
import {
  $activeSessionId,
  $currentCwd,
  $selectedStoredSessionId,
  $sessions,
  $workspaceCwdOwner,
  idsShareLineage,
  sessionMatchesStoredId
} from '@/store/session'
import { $focusedStoredSessionId, $sessionStates, $sessionTiles } from '@/store/session-states'

interface FocusedWorkspaceCwdInput {
  focusedRowCwd: string
  focusedStateCwd: string
  focusedStateStoredId: null | string
  focusedStoredSessionId: null | string
  liveCwdSharesFocusLineage: boolean
  primaryCwd: string
  selectedStoredSessionId: null | string
  workspaceCwdOwner: null | string
}

/**
 * Resolve the workspace shown by session-scoped chrome.
 *
 * A focused tile owns its own workspace and must never inherit the primary
 * chat's retained CWD. The primary may use `$currentCwd` only while the CWD's
 * ownership token still matches the selected session; this preserves the
 * existing fail-closed switch behavior while a new session is activating.
 */
export function resolveFocusedWorkspaceCwd({
  focusedRowCwd,
  focusedStateCwd,
  focusedStateStoredId,
  focusedStoredSessionId,
  liveCwdSharesFocusLineage,
  primaryCwd,
  selectedStoredSessionId,
  workspaceCwdOwner
}: FocusedWorkspaceCwdInput): string {
  const primaryFocused = !focusedStoredSessionId || focusedStoredSessionId === selectedStoredSessionId

  const liveCwdBelongsToFocus =
    Boolean(focusedStateCwd) &&
    (!focusedStoredSessionId ||
      !focusedStateStoredId ||
      focusedStateStoredId === focusedStoredSessionId ||
      liveCwdSharesFocusLineage)

  const primaryCwdBelongsToSelection = (workspaceCwdOwner ?? null) === (selectedStoredSessionId ?? null)

  return (
    (liveCwdBelongsToFocus ? focusedStateCwd : '') ||
    (primaryFocused && primaryCwdBelongsToSelection ? primaryCwd : '') ||
    focusedRowCwd ||
    ''
  ).trim()
}

/** Resolve one session surface's workspace without borrowing another session's CWD. */
export function useSessionWorkspaceCwd(focusedStoredSessionId: null | string): string {
  const primaryCwd = useStore($currentCwd)
  const workspaceCwdOwner = useStore($workspaceCwdOwner)
  const selectedStoredSessionId = useStore($selectedStoredSessionId)
  const primaryRuntimeId = useStore($activeSessionId)

  const tileRuntimeId = useStoreSelector($sessionTiles, tiles =>
    focusedStoredSessionId && focusedStoredSessionId !== selectedStoredSessionId
      ? (tiles.find(tile => tile.storedSessionId === focusedStoredSessionId)?.runtimeId ?? null)
      : null
  )

  const focusedRuntimeId =
    focusedStoredSessionId && focusedStoredSessionId !== selectedStoredSessionId ? tileRuntimeId : primaryRuntimeId

  const focusedStateCwd = useStoreSelector(
    $sessionStates,
    states => (focusedRuntimeId ? states[focusedRuntimeId]?.cwd?.trim() : '') || ''
  )

  const focusedStateStoredId = useStoreSelector(
    $sessionStates,
    states => (focusedRuntimeId ? states[focusedRuntimeId]?.storedSessionId?.trim() : null) || null
  )

  const focusedRowCwd = useStoreSelector($sessions, sessions => {
    if (!focusedStoredSessionId) {
      return ''
    }

    return sessions.find(session => sessionMatchesStoredId(session, focusedStoredSessionId))?.cwd?.trim() || ''
  })

  const liveCwdSharesFocusLineage = useStoreSelector($sessions, sessions => {
    if (!focusedStoredSessionId || !focusedStateStoredId) {
      return false
    }

    return idsShareLineage(focusedStoredSessionId, focusedStateStoredId, sessions)
  })

  return resolveFocusedWorkspaceCwd({
    focusedRowCwd,
    focusedStateCwd,
    focusedStateStoredId,
    focusedStoredSessionId,
    liveCwdSharesFocusLineage,
    primaryCwd,
    selectedStoredSessionId,
    workspaceCwdOwner
  })
}

/** The keyboard-focused chat's workspace, used by focus-sensitive status chrome. */
export function useFocusedWorkspaceCwd(): string {
  return useSessionWorkspaceCwd(useStore($focusedStoredSessionId))
}
