/**
 * Agent Screen — virtual display for macOS, controllable from the desktop.
 *
 * macOS has no Xvfb equivalent: there is no way to create a headless second
 * display. Agent Screen fills that gap with a tiny Swift companion app that
 * owns a CGVirtualDisplay (a REAL second display with its own Space), renders
 * it into a native window, and exposes it as an MJPEG stream on :8788. Any
 * window can be dragged onto it (native display shift) or teleported onto it
 * by dragging onto the window itself (drag portal).
 *
 * This plugin contributes:
 *  - a statusbar chip: monitor icon, green (#16A34A) while the app runs,
 *    gray when off; click toggles start/stop; hover shows a live preview of
 *    the virtual screen (only while active — no popover when off).
 *  - a snappable pane in the desktop layout (PANES_AREA) showing the live
 *    stream, dockable left/right/bottom via drag & drop like session panes.
 *
 * Backend: plugins/agent-screen/dashboard/plugin_api.py (REST router mounted
 * at /api/plugins/agent-screen/ by the dashboard plugin system; native
 * companion sources + build script live in plugins/agent-screen/native/).
 */

import {
  cn,
  icons,
  PANES_AREA,
  Popover,
  PopoverContent,
  PopoverTrigger,
  STATUSBAR_AREAS,
  StatusDot,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useRef, useState } from 'react'

const ID = 'agent-screen'
const STREAM_URL = 'http://127.0.0.1:8788/stream.mjpeg'

const GREEN = '#16A34A'
const GRAY = 'var(--ui-text-tertiary)'

type Rest = <T>(path: string, opts?: { method?: string; body?: unknown }) => Promise<T>

let rest: null | Rest = null

function useAgentStatus() {
  return useQuery({
    queryKey: [ID, 'status'],
    queryFn: () => (rest ? rest<{ running: boolean; stream: boolean }>('/status') : Promise.reject(new Error('agent-screen api not ready'))),
    refetchInterval: 5000,
    staleTime: 2000
  })
}

function useToggle() {
  const qc = useQueryClient()

  return useMutation({
    mutationFn: async () => {
      // Read status fresh — don't trust the (possibly 5s stale) cache.
      const s = await (rest ? rest<{ running: boolean }>('/status') : Promise.reject(new Error('agent-screen api not ready')))

      return rest ? rest(s && s.running ? '/stop' : '/start', { method: 'POST' }) : Promise.reject(new Error('agent-screen api not ready'))
    },
    onSettled: () => qc.invalidateQueries({ queryKey: [ID, 'status'] })
  })
}

// ---------------------------------------------------------------------------
// Statusbar chip
// ---------------------------------------------------------------------------

function AgentScreenChip() {
  const { data } = useAgentStatus()
  const toggle = useToggle()
  const [hover, setHover] = useState(false)
  const openTimer = useRef<number | null>(null)
  const closeTimer = useRef<number | null>(null)

  const running = !!(data && data.running)

  const onMouseEnter = () => {
    if (closeTimer.current != null) {clearTimeout(closeTimer.current)}
    openTimer.current = window.setTimeout(() => setHover(true), 150)
  }

  const onMouseLeave = () => {
    if (openTimer.current != null) {clearTimeout(openTimer.current)}
    closeTimer.current = window.setTimeout(() => setHover(false), 120)
  }

  return (
    <Popover
      onOpenChange={setHover}
      open={hover && running}
    >
      <PopoverTrigger asChild>
        <button
          aria-label={running ? 'Agent Screen: aktiv — Klick zum Stoppen' : 'Agent Screen: aus — Klick zum Starten'}
          className={cn(
            'inline-flex h-full items-center gap-1 px-1.5 transition-colors',
            'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
          )}
          // preventDefault: the click toggles the app, NOT the popover
          onClick={(e) => {
            e.preventDefault()

            if (!toggle.isPending) {toggle.mutate()}
          }}
          onMouseEnter={onMouseEnter}
          onMouseLeave={onMouseLeave}
          type="button"
        >
          <icons.Monitor size={14} style={{ color: running ? GREEN : GRAY, transition: 'color 200ms' }} />
        </button>
      </PopoverTrigger>
      {/* Preview ONLY when active — otherwise the popover is not rendered at all */}
      {running && (
        <PopoverContent align="end" className="w-auto p-1.5" side="top" sideOffset={6}>
          <img
            alt="Agent Screen (live)"
            src={STREAM_URL}
            style={{
              width: 320,
              aspectRatio: '16 / 9',
              objectFit: 'contain',
              borderRadius: 6,
              background: '#000'
            }}
          />
        </PopoverContent>
      )}
    </Popover>
  )
}

// ---------------------------------------------------------------------------
// Snappable pane in the desktop layout (live view)
// ---------------------------------------------------------------------------

function AgentScreenPane() {
  const { data } = useAgentStatus()
  const toggle = useToggle()
  const running = !!(data && data.running)
  const streaming = !!(data && data.stream)

  return (
    <div className="flex h-full flex-col gap-1.5 p-2 text-sm">
      {/* Header: status + toggle */}
      <div className="flex items-center gap-2 px-1">
        <StatusDot tone={streaming ? 'good' : 'muted'} />
        <span className="font-medium">Agent Screen</span>
        <span className="text-(--ui-text-tertiary)">
          {streaming ? 'live · :8788' : running ? 'startet …' : 'aus'}
        </span>
        <button
          className={cn(
            'ml-auto inline-flex items-center gap-1 rounded px-2 py-0.5 text-[0.6875rem] transition-colors',
            'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-foreground'
          )}
          onClick={() => { if (!toggle.isPending) {toggle.mutate()} }}
          type="button"
        >
          {running ? 'Stoppen' : 'Starten'}
        </button>
      </div>
      {/* Live stream or offline hint */}
      {streaming ? (
        <img
          alt="Agent Screen (live)"
          src={STREAM_URL}
          style={{
            width: '100%',
            flex: 1,
            minHeight: 0,
            objectFit: 'contain',
            borderRadius: 6,
            background: '#000'
          }}
        />
      ) : (
        <div className="flex flex-1 items-center justify-center text-(--ui-text-tertiary)">
          {running
            ? 'Stream läuft noch nicht …'
            : 'Agent Screen ist aus. Klick „Starten" — das native Fenster öffnet sich.'}
        </div>
      )}
    </div>
  )
}

export default {
  id: ID,
  name: 'Agent Screen',
  // Ships OFF by default: it needs the native companion built & installed
  // (see plugins/agent-screen/README.md). Inventories in Settings ▸ Plugins
  // and registers nothing until the user flips the switch.
  defaultEnabled: false,
  register(ctx: {
    rest: Rest
    register: (c: unknown) => void
  }) {
    rest = ctx.rest

    // Statusbar chip (toggle + hover preview)
    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 120,
      render: () => <AgentScreenChip />
    })

    // Snappable pane in the desktop layout: starts on the right, dockable
    // anywhere via drag & drop (left/right/bottom), stays where the user
    // puts it.
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'Agent Screen',
      data: {
        placement: 'right',
        dock: { pane: 'workspace', pos: 'right' },
        width: '360px'
      },
      render: () => <AgentScreenPane />
    })
  }
}
