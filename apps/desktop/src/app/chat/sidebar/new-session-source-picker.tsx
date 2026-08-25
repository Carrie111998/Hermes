import { useStore } from '@nanostores/react'
import { Fragment } from 'react'

import { Codicon } from '@/components/ui/codicon'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { Tip } from '@/components/ui/tooltip'
import type { DesktopRegistryConnection } from '@/global'
import { useI18n } from '@/i18n'
import { sortConnectionsForDisplay } from '@/lib/connection-display'
import { Check, Cloud, Monitor, Network, Terminal } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { $activeConnectionId, $connectionsRegistry, $hasMultipleConnections } from '@/store/connections'

import { WorkspaceAddButton } from './projects/workspace-header'

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

  const KIND_LABEL: Record<DesktopRegistryConnection['kind'], string> = {
    local: t.settings.connections.kindLocal,
    remote: t.settings.connections.kindRemote,
    cloud: t.settings.connections.kindCloud,
    ssh: t.settings.connections.kindSsh
  }

  const sorted = sortConnectionsForDisplay(connections)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64 p-1" data-source-picker="" side="right">
        <DropdownMenuLabel className="px-2 py-1.5">
          {t.settings.connections.title}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {sorted.map((connection, index) => {
          const Icon = KIND_ICON[connection.kind]
          const isActive = connection.id === activeConnectionId
          const kindLabel = KIND_LABEL[connection.kind]

          return (
            <Fragment key={connection.id}>
              {index > 0 && <DropdownMenuSeparator className="my-0.5" />}
              <DropdownMenuItem
                aria-pressed={isActive}
                className={cn(
                  'flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5',
                  isActive && 'bg-(--ui-control-active-background) text-foreground'
                )}
                onSelect={() => onPick(connection.id)}
              >
                <Icon
                  className={cn(
                    'size-4 shrink-0',
                    isActive ? 'text-(--ui-accent)' : 'text-(--ui-text-tertiary)'
                  )}
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[0.8125rem] leading-tight">
                    {connection.label}
                  </span>
                  <span
                    className={cn(
                      'block truncate text-[0.6875rem] leading-tight',
                      isActive ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-tertiary)'
                    )}
                  >
                    {kindLabel}
                  </span>
                </span>
                {isActive && <Check className="size-4 shrink-0 text-(--ui-accent)" />}
              </DropdownMenuItem>
            </Fragment>
          )
        })}
        {sorted.length === 0 && (
          <DropdownMenuItem
            className="cursor-default px-2 py-1.5 text-[0.8125rem] text-(--ui-text-tertiary)"
            disabled
          >
            {t.settings.connections.emptySources}
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

const ADD_BTN_CLASS =
  'grid size-4 shrink-0 place-items-center rounded-sm bg-transparent text-(--ui-text-quaternary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/workspace:opacity-100 data-[state=open]:opacity-100'

/**
 * Workspace/date "+" that opens the per-session source picker when more than
 * one gateway is registered. Single-source installs keep the one-click add.
 */
export function SourceAwareAddButton({
  label,
  onNewSession,
  onPickSource
}: {
  label: string
  onNewSession: () => void
  onPickSource?: (connectionId: string) => void
}) {
  const hasMultiple = useStore($hasMultipleConnections)
  const registry = useStore($connectionsRegistry)
  const activeConnectionId = useStore($activeConnectionId)

  if (hasMultiple && onPickSource) {
    return (
      <Tip label={label}>
        <NewSessionSourcePicker
          activeConnectionId={activeConnectionId}
          connections={registry?.connections ?? []}
          onPick={onPickSource}
          trigger={
            <button aria-label={label} className={ADD_BTN_CLASS} type="button">
              <Codicon name="add" size="0.75rem" />
            </button>
          }
        />
      </Tip>
    )
  }

  return <WorkspaceAddButton label={label} onClick={onNewSession} />
}
