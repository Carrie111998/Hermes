/**
 * Pool plugin — manage and monitor multiple Hermes connections from the desktop app.
 *
 * Displays configured connections, their status (online/offline), and provides
 * add/remove/switch/test actions. Shares ~/.hermes/connections.yaml with the TUI.
 *
 * Ships OFF by default (defaultEnabled: false) — opt-in via Settings → Plugins.
 */

import {
  type HermesPlugin,
  host,
  PANES_AREA,
  PALETTE_AREA,
  type PaletteContribution,
  STATUSBAR_AREAS,
  Tip,
  atom,
  cn,
  useValue,
} from '@hermes/plugin-sdk'

// ─── Types ──────────────────────────────────────────────────────────────────

interface PoolConnection {
  name: string
  url: string
  mode: string
  auth: string
  active: boolean
  status: string
  last_error?: string
}

interface PoolListResponse {
  connections: PoolConnection[]
}

interface PoolMutationResponse {
  message: string
  url?: string
  count?: number
}

// ─── Plugin-local state ─────────────────────────────────────────────────────

const $connections = atom<PoolConnection[]>([])
const $activeName = atom<string | null>(null)
const $loading = atom(false)
const $error = atom<string | null>(null)

// ─── Helpers ────────────────────────────────────────────────────────────────

async function refreshConnections() {
  $loading.set(true)
  $error.set(null)
  try {
    const res = await host.request<PoolListResponse>('pool.list', {})
    $connections.set(res.connections)
    const active = res.connections.find((c) => c.active)
    $activeName.set(active?.name ?? null)
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e)
    // Fallback: connections.yaml may not exist yet or pool.list may not be available
    $error.set(msg)
  } finally {
    $loading.set(false)
  }
}

async function callPool(method: string, params: Record<string, unknown>): Promise<string> {
  try {
    const res = await host.request<PoolMutationResponse>(method, params)
    await refreshConnections()
    return res.message
  } catch (e: unknown) {
    return e instanceof Error ? e.message : String(e)
  }
}

// ─── Components ─────────────────────────────────────────────────────────────

function StatusDot({ status }: { status: string }) {
  const color =
    status === 'online'
      ? 'bg-green-500'
      : status === 'offline'
        ? 'bg-red-500'
        : 'bg-yellow-500'
  return (
    <span
      className={cn('inline-block h-2 w-2 rounded-full', color)}
      title={status}
    />
  )
}

function ConnectionRow({ conn }: { conn: PoolConnection }) {
  const active = conn.active
  return (
    <div
      className={cn(
        'flex items-center gap-2 rounded px-2 py-1.5 text-sm',
        active ? 'bg-(--chrome-action-hover)' : 'hover:bg-(--chrome-action-hover)'
      )}
    >
      <StatusDot status={conn.status} />
      <span className={cn('flex-1 truncate', active && 'font-medium')}>
        {conn.name}
        {active && <span className="ml-1 text-(--ui-text-tertiary)">(active)</span>}
      </span>
      <span className="truncate text-(--ui-text-tertiary)" title={conn.url}>
        {conn.url}
      </span>
      {conn.last_error && (
        <Tip label={conn.last_error}>
          <span className="text-(--ui-text-tertiary)">⚠</span>
        </Tip>
      )}
    </div>
  )
}

function PoolPane() {
  const connections = useValue($connections)
  const activeName = useValue($activeName)
  const loading = useValue($loading)
  const error = useValue($error)

  return (
    <div className="flex h-full flex-col gap-2 p-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-medium">Connections</h2>
        <Tip label="Refresh connection status">
          <button
            type="button"
            className="rounded px-1.5 text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover)"
            onClick={() => refreshConnections()}
          >
            ↻
          </button>
        </Tip>
      </div>

      {error && (
        <div className="rounded bg-(--ui-surface-danger) px-2 py-1 text-xs text-(--ui-text-danger)">
          {error}
        </div>
      )}

      {/* Connection list */}
      <div className="flex-1 overflow-y-auto">
        {loading && connections.length === 0 && (
          <div className="px-2 py-4 text-center text-sm text-(--ui-text-tertiary)">
            Loading…
          </div>
        )}
        {!loading && connections.length === 0 && (
          <div className="px-2 py-4 text-center text-sm text-(--ui-text-tertiary)">
            No connections configured.{' '}
            <button
              type="button"
              className="text-(--ui-accent) underline"
              onClick={() => {
                const name = prompt('Connection name:')
                if (!name) return
                const url = prompt('Connection URL (e.g. https://homelab.tail.ts.net):')
                if (!url) return
                callPool('pool.add', { name, url })
              }}
            >
              Add one
            </button>{' '}
            or run <code className="rounded bg-(--ui-surface-secondary) px-1">/pool discover</code>.
          </div>
        )}
        {connections.map((conn) => (
          <ConnectionRow key={conn.name} conn={conn} />
        ))}
      </div>

      {/* Actions */}
      <div className="flex flex-col gap-1 border-t border-(--ui-stroke-secondary) pt-2">
        <button
          type="button"
          className="rounded px-2 py-1 text-left text-sm hover:bg-(--chrome-action-hover)"
          onClick={() => {
            const name = prompt('Connection name:')
            if (!name) return
            const url = prompt('Connection URL:')
            if (!url) return
            const token = prompt('Token (optional, press Enter to skip):') || undefined
            callPool('pool.add', { name, url, token })
          }}
        >
          + Add connection
        </button>
        <button
          type="button"
          className="rounded px-2 py-1 text-left text-sm hover:bg-(--chrome-action-hover)"
          onClick={() => {
            const name = prompt('Connection name to remove:')
            if (!name) return
            callPool('pool.remove', { name })
          }}
        >
          − Remove connection
        </button>
        <button
          type="button"
          className="rounded px-2 py-1 text-left text-sm hover:bg-(--chrome-action-hover)"
          onClick={() => {
            const name = prompt('Connection name to switch to:')
            if (!name) return
            callPool('pool.switch', { name })
          }}
        >
          ⇄ Switch active
        </button>
        <button
          type="button"
          className="rounded px-2 py-1 text-left text-sm hover:bg-(--chrome-action-hover)"
          onClick={() => {
            const name = prompt('Connection name to test:')
            if (!name) return
            callPool('pool.test', { name })
          }}
        >
          ✓ Test connection
        </button>
        <button
          type="button"
          className="rounded px-2 py-1 text-left text-sm text-(--ui-accent) hover:bg-(--chrome-action-hover)"
          onClick={() => callPool('pool.discover', {})}
        >
          ⟳ Discover Tailscale instances
        </button>
      </div>
    </div>
  )
}

// ─── Status bar chip ────────────────────────────────────────────────────────

function PoolChip() {
  const activeName = useValue($activeName)
  const count = useValue($connections).length

  return (
    <Tip label={`${count} connection(s) configured`}>
      <button
        type="button"
        className="inline-flex h-full items-center gap-1 rounded-none px-1.5 text-[0.6875rem] tabular-nums transition-colors text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground"
        onClick={() => refreshConnections()}
      >
        <span className="h-2 w-2 rounded-full bg-green-500" />
        {activeName || 'no active'}
      </button>
    </Tip>
  )
}

// ─── Plugin export ──────────────────────────────────────────────────────────

const POOL_PALETTE_ACTION: PaletteContribution = {
  id: 'pool-refresh',
  label: 'Pool: Refresh connections',
  perform: () => refreshConnections(),
}

export default {
  id: 'pool',
  name: 'Connection Pool',
  defaultEnabled: false,
  register(ctx) {
    // Pane
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'Pool',
      data: { placement: 'right', width: '300px' },
      render: () => <PoolPane />,
    })

    // Status bar chip
    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 140,
      render: () => <PoolChip />,
    })

    // Palette command
    ctx.register({
      id: 'palette',
      area: PALETTE_AREA,
      data: POOL_PALETTE_ACTION,
    })

    // Initial load
    refreshConnections()
  },
} satisfies HermesPlugin
