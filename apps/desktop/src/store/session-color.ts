import { computed } from 'nanostores'

import { sessionProjectColor } from '@/app/chat/sidebar/projects/workspace-groups'
import { persistentAtom } from '@/lib/persisted'
import { parseSessionIdentityKey, sessionIdentityKey } from '@/lib/session-identity'
import { $projects } from '@/store/projects'
import { $sessions, sessionPinId } from '@/store/session'
import type { ProjectInfo, SessionInfo } from '@/types/hermes'

// Per-session color OVERRIDES — a user-picked color that wins over the inherited
// project color (#66565 layer 2). Desktop-local like pins, keyed by the DURABLE
// lineage id so a color survives auto-compression's session-id rotation. To take
// this to the TUI later, promote this one atom to a backend SessionInfo.color
// field — the resolver below and the picker UI stay exactly as they are.
const SESSION_COLOR_OVERRIDES_VERSION = 1

interface PersistedSessionColorOverride {
  color: string
  profile: string
  storedSessionId: string
}

function isPersistedSessionColorOverride(value: unknown): value is PersistedSessionColorOverride {
  return (
    Boolean(value) &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    typeof (value as PersistedSessionColorOverride).color === 'string' &&
    typeof (value as PersistedSessionColorOverride).profile === 'string' &&
    typeof (value as PersistedSessionColorOverride).storedSessionId === 'string' &&
    (value as PersistedSessionColorOverride).storedSessionId.length > 0
  )
}

export function decodeSessionColorOverrides(raw: string): Record<string, string> {
  const parsed = JSON.parse(raw) as unknown
  const stored = parsed as { entries?: unknown; version?: unknown } | null

  if (
    stored &&
    typeof stored === 'object' &&
    stored.version === SESSION_COLOR_OVERRIDES_VERSION &&
    Array.isArray(stored.entries)
  ) {
    return Object.fromEntries(
      stored.entries
        .filter(isPersistedSessionColorOverride)
        .map(entry => [sessionIdentityKey(entry.storedSessionId, entry.profile), entry.color])
    )
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return {}
  }

  // Main's legacy schema was an ownerless string record. Every key is an
  // opaque default-profile id, even when its bytes resemble a compound key.
  return Object.fromEntries(
    Object.entries(parsed)
      .filter((entry): entry is [string, string] => typeof entry[1] === 'string')
      .map(([storedSessionId, color]) => [sessionIdentityKey(storedSessionId, 'default'), color])
  )
}

export function encodeSessionColorOverrides(overrides: Record<string, string>): string {
  return JSON.stringify({
    entries: Object.entries(overrides).map(([identityKey, color]) => ({
      ...parseSessionIdentityKey(identityKey),
      color
    })),
    version: SESSION_COLOR_OVERRIDES_VERSION
  })
}

export const $sessionColorOverrides = persistentAtom<Record<string, string>>('hermes.desktop.sessionColors', {}, {
  decode: decodeSessionColorOverrides,
  encode: encodeSessionColorOverrides
})

// Set a session's override (null clears it → falls back to the project color).
export function setSessionColorOverride(durableId: string, color: null | string): void {
  const prev = $sessionColorOverrides.get()

  if (color) {
    $sessionColorOverrides.set({ ...prev, [durableId]: color })
  } else if (durableId in prev) {
    const next = { ...prev }
    delete next[durableId]
    $sessionColorOverrides.set(next)
  }
}

// The resolved color for every session, keyed by live session id — the ONE
// source of truth both the sidebar rows and the pane tabs read, so the two
// surfaces can never drift. Recomputed only when the session list, projects, or
// overrides change (all cold atoms; the working/streaming pulse lives in
// $sessionStates, so a busy flip never rebuilds this), and every consumer reads
// it as an O(1) lookup rather than re-deriving membership per render.
//
// Precedence in one place: an explicit per-session override wins over the
// inherited project color. Agent-set color (#66565 layer 3) slots in here too.
function resolveSessionColor(
  session: SessionInfo,
  projects: ProjectInfo[],
  overrides: Record<string, string>
): string | undefined {
  return overrides[sessionPinId(session)] ?? sessionProjectColor(session, projects) ?? undefined
}

export const $sessionColorById = computed(
  [$sessions, $projects, $sessionColorOverrides],
  (sessions, projects, overrides) => {
    const map: Record<string, string> = {}

    for (const session of sessions) {
      const color = resolveSessionColor(session, projects, overrides)

      if (color) {
        map[sessionIdentityKey(session.id, session.profile)] = color
      }
    }

    return map
  }
)

// The color for a single session object (the tabs already hold the SessionInfo
// they render, so they resolve through the same map the sidebar reads). A row
// that isn't in `$sessions` — e.g. a project-tree session older than the
// paginated recents page, opened as a tab — misses the map, so fall back to the
// same resolver the map is built from.
export function sessionColorFor(session: null | SessionInfo | undefined): string | undefined {
  if (!session) {
    return undefined
  }

  return (
    $sessionColorById.get()[sessionIdentityKey(session.id, session.profile)] ??
    resolveSessionColor(session, $projects.get(), $sessionColorOverrides.get())
  )
}
