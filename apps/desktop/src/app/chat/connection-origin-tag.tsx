import { Tip } from '@/components/ui/tooltip'
import type { DesktopRegistryConnection } from '@/global'
import { useI18n } from '@/i18n'
import { Cloud, Monitor, Network, Terminal } from '@/lib/icons'
import { cn } from '@/lib/utils'

const KIND_ICON: Record<DesktopRegistryConnection['kind'], typeof Monitor> = {
  local: Monitor,
  remote: Network,
  cloud: Cloud,
  ssh: Terminal
}

/**
 * Gateway-origin chip: a session's owning connection as an icon + label. The
 * mirror of {@link ProfileTag} (#66003) but on the CONNECTION axis — a row
 * that runs on a remote/SSH/cloud gateway shows a small badge naming that
 * gateway, so browsing a foreign connection's sessions is explicit instead of
 * silent. Local "This device" sessions need no badge (the normal case) and
 * are gated out at the call site.
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

  const kindKey =
    connection.kind === 'local'
      ? 'kindLocal'
      : connection.kind === 'remote'
        ? 'kindRemote'
        : connection.kind === 'cloud'
          ? 'kindCloud'
          : 'kindSsh'

  const label = `${connection.label} · ${t.settings.connections[kindKey]}`

  return (
    <Tip label={label}>
      <span
        aria-label={label}
        className={cn(
          'inline-flex items-center gap-1 rounded-[4px] bg-(--ui-control-active-background)/50 px-1 py-0.5',
          'text-[0.625rem] leading-none text-(--ui-text-secondary)',
          className
        )}
        role="img"
      >
        <Icon className="size-3 shrink-0 text-(--ui-text-tertiary)" />
        <span className="max-w-28 truncate">{connection.label}</span>
      </span>
    </Tip>
  )
}