import { Tip } from '@/components/ui/tooltip'
import type { DesktopConnectionsRegistry, DesktopRegistryConnection } from '@/global'
import { useI18n } from '@/i18n'
import { Cloud, Monitor, Network, Terminal } from '@/lib/icons'
import { cn } from '@/lib/utils'

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
 * Gateway-origin mark: icon + label naming the connection a session/group runs
 * on. Quiet by design (Cursor's gray host suffix) so a mixed list stays
 * scannable. Local "This device" is gated out at the call site.
 */
export function ConnectionOriginTag({
  className,
  connection
}: {
  className?: string
  connection: DesktopRegistryConnection
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
        <Icon className="size-3 shrink-0" />
        <span className="max-w-28 truncate">{connection.label}</span>
      </span>
    </Tip>
  )
}