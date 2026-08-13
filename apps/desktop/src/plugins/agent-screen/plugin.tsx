/**
 * Agent Screen — virtual display for macOS, controllable from the desktop.
 *
 * A fork of DeskPad (Bastian Andelefski / Stengo, MIT 2022) plus a Hermes
 * chip, pane, and loopback MJPEG preview. See plugins/agent-screen/NOTICE.
 *
 * Local macOS backend only: start/stop hit the connected gateway; the
 * preview always reads http://127.0.0.1:8788 on this machine. Remote /
 * non-darwin backends get a disabled chip, not a surprise spawn.
 */

import {
  cn,
  type HermesPlugin,
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

import { AGENT_SCREEN_LOCALES, useAgentScreenText } from './i18n'

const ID = 'agent-screen'
const STREAM_URL = 'http://127.0.0.1:8788/stream.mjpeg'
const PING_URL = 'http://127.0.0.1:8788/ping'

const GREEN = '#16A34A'
const GRAY = 'var(--ui-text-tertiary)'

type Rest = <T>(path: string, opts?: { method?: string; body?: unknown }) => Promise<T>

type AgentStatus = {
  running: boolean
  stream: boolean
  supported?: boolean
  platform?: string
  error?: string
}

let rest: null | Rest = null

async function pingLocalStream(): Promise<boolean> {
  try {
    const r = await fetch(PING_URL, { signal: AbortSignal.timeout(800) })
    return r.ok && (await r.text()).trim() === 'ok'
  } catch {
    return false
  }
}

function useAgentStatus() {
  return useQuery({
    queryKey: [ID, 'status'],
    queryFn: async (): Promise<AgentStatus & { localStream: boolean }> => {
      if (!rest) {
        throw new Error('agent-screen api not ready')
      }
      const s = await rest<AgentStatus>('/status')
      const localStream = await pingLocalStream()
      return { ...s, localStream }
    },
    refetchInterval: 5000,
    staleTime: 2000
  })
}

function useToggle() {
  const qc = useQueryClient()

  return useMutation({
    mutationFn: async () => {
      if (!rest) {
        throw new Error('agent-screen api not ready')
      }
      const s = await rest<AgentStatus>('/status')
      if (s.supported === false) {
        throw new Error(s.error || 'Agent Screen requires a local macOS backend')
      }
      return rest(s.running ? '/stop' : '/start', { method: 'POST' })
    },
    onSettled: () => qc.invalidateQueries({ queryKey: [ID, 'status'] })
  })
}

function AgentScreenChip() {
  const { data } = useAgentStatus()
  const toggle = useToggle()
  const t = useAgentScreenText()
  const [hover, setHover] = useState(false)
  const openTimer = useRef<number | null>(null)
  const closeTimer = useRef<number | null>(null)

  const supported = data?.supported !== false
  const running = !!(data && data.running)
  const preview = !!(data && data.localStream)

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
      open={hover && running && preview}
    >
      <PopoverTrigger asChild>
        <button
          aria-label={supported ? (running ? t.chipOn : t.chipOff) : t.chipUnsupported}
          className={cn(
            'inline-flex h-full items-center gap-1 px-1.5 transition-colors',
            'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground',
            !supported && 'opacity-50'
          )}
          disabled={!supported}
          onClick={(e) => {
            e.preventDefault()
            if (!supported || toggle.isPending) {return}
            toggle.mutate()
          }}
          onMouseEnter={onMouseEnter}
          onMouseLeave={onMouseLeave}
          type="button"
        >
          <icons.Monitor size={14} style={{ color: running && supported ? GREEN : GRAY, transition: 'color 200ms' }} />
        </button>
      </PopoverTrigger>
      {running && preview && (
        <PopoverContent align="end" className="w-auto p-1.5" side="top" sideOffset={6}>
          <img
            alt={t.previewAlt}
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

function AgentScreenPane() {
  const { data } = useAgentStatus()
  const toggle = useToggle()
  const t = useAgentScreenText()
  const supported = data?.supported !== false
  const running = !!(data && data.running)
  const streaming = !!(data && data.localStream)

  return (
    <div className="flex h-full flex-col gap-1.5 p-2 text-sm">
      <div className="flex items-center gap-2 px-1">
        <StatusDot tone={streaming ? 'good' : 'muted'} />
        <span className="font-medium">{t.name}</span>
        <span className="text-(--ui-text-tertiary)">
          {!supported ? t.chipUnsupported : streaming ? t.live : running ? t.starting : t.off}
        </span>
        <button
          className={cn(
            'ml-auto inline-flex items-center gap-1 rounded px-2 py-0.5 text-[0.6875rem] transition-colors',
            'text-(--ui-text-secondary) hover:bg-(--chrome-action-hover) hover:text-foreground',
            !supported && 'opacity-50'
          )}
          disabled={!supported || toggle.isPending}
          onClick={() => { if (supported && !toggle.isPending) {toggle.mutate()} }}
          type="button"
        >
          {running ? t.stop : t.start}
        </button>
      </div>
      {streaming ? (
        <img
          alt={t.previewAlt}
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
          {!supported ? t.unsupported : running ? t.streamWait : t.offHint}
        </div>
      )}
    </div>
  )
}

const plugin: HermesPlugin = {
  id: ID,
  name: 'Agent Screen',
  description: 'Virtual macOS display (DeskPad fork) — second screen, live preview, local backend only.',
  defaultEnabled: false,
  register(ctx) {
    rest = ctx.rest
    ctx.onDispose(() => {
      rest = null
    })
    ctx.i18n.register(AGENT_SCREEN_LOCALES)

    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 120,
      render: () => <AgentScreenChip />
    })

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

export default plugin
