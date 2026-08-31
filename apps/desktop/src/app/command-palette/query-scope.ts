import type { HermesConnection } from '@/global'

type PaletteConnection = Pick<HermesConnection, 'baseUrl' | 'connectionId' | 'mode' | 'profile' | 'remoteIdentity'>

/**
 * Query keys for palette data. Keep every routing identity as its own key part:
 * joining them into a string would make values containing `:` collide, and the
 * active profile can change while a shared gateway descriptor stays unchanged.
 */
export function commandPaletteQueryKey(
  source: 'archived' | 'config' | 'sessions',
  connection: PaletteConnection | null | undefined,
  activeProfile: string | null | undefined
) {
  return [
    'command-palette',
    source,
    connection?.connectionId ?? '',
    connection?.mode ?? 'local',
    connection?.baseUrl ?? '',
    connection?.remoteIdentity ?? '',
    connection?.profile ?? '',
    activeProfile?.trim() || 'default'
  ] as const
}
