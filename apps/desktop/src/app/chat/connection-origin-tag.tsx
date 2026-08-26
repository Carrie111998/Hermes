import { useStore } from '@nanostores/react'

import { Tip } from '@/components/ui/tooltip'
import type { DesktopConnectionsRegistry, DesktopRegistryConnection } from '@/global'
import { useI18n } from '@/i18n'
import { Cloud, Monitor, Network, Terminal } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { $connectionsRegistry } from '@/store/connections'
import { $sessions, sessionMatchesStoredId } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

const KIND_ICON: Record<DesktopRegistryConnection['kind'], typeof Monitor> = {
  local: Monitor,
  remote: Network,
  cloud: Cloud,
  ssh: Terminal
}

const KIND_KEY = {
  local: 'kindLocal',
  remote: 'kindRemote',
  cloud: 'kindCloud',
  ssh: 'kindSsh'
} as const

/** The gateway a row/group should name, or null when it is the local default. */
export function visibleSessionOrigin(
  session: { connection_id?: string },
  registry: DesktopConnectionsRegistry | null | undefined,
  activeConnectionId: string | null,
  section?: DesktopRegistryConnection | null
): DesktopRegistryConnection | null {
  const origin =
    (section && section.kind !== 'local' ? section : null) ??
    registry?.connections.find(connection => connection.id === (session.connection_id || activeConnectionId)) ??
    null

  return origin && origin.kind !== 'local' ? origin : null
}

/** Shared foreign origin when every session in a group runs on the same gateway. */
export function sharedSessionsOrigin(
  sessions: { connection_id?: string }[],
  registry: DesktopConnectionsRegistry | null | undefined,
  activeConnectionId: string | null
): DesktopRegistryConnection | null {
  if (sessions.length === 0) {
    return null
  }

  const first = visibleSessionOrigin(sessions[0], registry, activeConnectionId)

  if (!first) {
    return null
  }

  for (const session of sessions) {
    if (visibleSessionOrigin(session, registry, activeConnectionId)?.id !== first.id) {
      return null
    }
  }

  return first
}

/**
 * Gateway-origin mark: names the connection a session/group runs on. Quiet by
 * design (Cursor's gray host suffix) so a mixed list stays scannable. Local
 * "This device" is gated out at the call site.
 *
 * `quiet` — label only (group headers). `iconOnly` — glyph only (tab chrome).
 */
export function ConnectionOriginTag({
  className,
  connection,
  iconOnly = false,
  quiet = false
}: {
  className?: string
  connection: DesktopRegistryConnection
  iconOnly?: boolean
  quiet?: boolean
}) {
  const { t } = useI18n()
  const Icon = KIND_ICON[connection.kind]
  const label = `${connection.label} · ${t.settings.connections[KIND_KEY[connection.kind]]}`

  return (
    <Tip label={label}>
      <span
        aria-label={label}
        className={cn(
          'inline-flex min-w-0 items-center gap-0.5 text-[0.625rem] leading-none text-(--ui-text-quaternary)',
          className
        )}
        data-connection-kind={connection.kind}
        data-slot="connection-origin-tag"
        role="img"
      >
        {!quiet && <Icon className="size-3 shrink-0" />}
        {!iconOnly && <span className="max-w-28 truncate">{connection.label}</span>}
      </span>
    </Tip>
  )
}

/** Compact origin mark for a session tab. Local stays unlabeled. */
export function SessionOriginTabMark({ storedSessionId }: { storedSessionId: string | null | undefined }) {
  const registry = useStore($connectionsRegistry)
  const tiles = useStore($sessionTiles)
  const sessions = useStore($sessions)

  const connectionId =
    tiles.find(tile => tile.storedSessionId === storedSessionId)?.ownerRoute?.connectionId ||
    sessions.find(session => storedSessionId != null && sessionMatchesStoredId(session, storedSessionId))?.connection_id

  const connection = connectionId ? (registry?.connections.find(row => row.id === connectionId) ?? null) : null

  if (!connection || connection.kind === 'local') {
    return null
  }

  return <ConnectionOriginTag connection={connection} iconOnly />
}
