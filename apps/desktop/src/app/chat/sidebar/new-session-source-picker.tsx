import { Fragment } from 'react'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import type { DesktopRegistryConnection } from '@/global'
import { useI18n } from '@/i18n'
import { sortConnectionsForDisplay } from '@/lib/connection-display'
import { Check, Cloud, Monitor, Network, Terminal } from '@/lib/icons'
import { cn } from '@/lib/utils'

const KIND_ICON: Record<DesktopRegistryConnection['kind'], typeof Monitor> = {
  local: Monitor,
  remote: Network,
  cloud: Cloud,
  ssh: Terminal
}

/**
 * Per-session source picker: choose which registered connection (Local /
 * Remote gateway / SSH / Hermes Cloud) a brand-new session runs on, instead of
 * always creating it on the active agent. Only mounted when more than one
 * connection is registered — single-source installs keep the plain one-click
 * "new session" behavior and never see this.
 */
export function NewSessionSourcePicker({
  trigger,
  connections,
  activeConnectionId,
  onPick
}: {
  trigger: React.ReactNode
  connections: DesktopRegistryConnection[]
  activeConnectionId: string | null
  onPick: (connectionId: string) => void
}) {
  const { t } = useI18n()
  const sorted = sortConnectionsForDisplay(connections)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64" side="right">
        <DropdownMenuLabel className="px-3 py-2">
          {t.settings.connections.title}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {sorted.map((connection, index) => {
          const Icon = KIND_ICON[connection.kind]
          const isActive = connection.id === activeConnectionId

          const kindLabel =
            connection.kind === 'local'
              ? t.settings.connections.kindLocal
              : connection.kind === 'remote'
                ? t.settings.connections.kindRemote
                : connection.kind === 'cloud'
                  ? t.settings.connections.kindCloud
                  : t.settings.connections.kindSsh

          return (
            <Fragment key={connection.id}>
              {index > 0 && <DropdownMenuSeparator className="my-0.5" />}
              <DropdownMenuItem
                className={cn('flex items-center gap-2 px-3 py-1.5', isActive && 'text-foreground')}
                onSelect={() => onPick(connection.id)}
              >
                <Icon className="size-4 shrink-0 text-(--ui-text-tertiary)" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[0.8125rem] leading-tight">{connection.label}</span>
                  <span className="block truncate text-[0.6875rem] leading-tight text-(--ui-text-tertiary)">
                    {kindLabel}
                  </span>
                </span>
                {isActive && <Check className="size-4 shrink-0 text-(--ui-text-tertiary)" />}
              </DropdownMenuItem>
            </Fragment>
          )
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
