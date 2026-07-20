import type { ToolPreset } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { SETTINGS_ROUTE } from '@/app/routes'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { Tip } from '@/components/ui/tooltip'
import { ChevronDown, Wrench } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { activeGateway } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { $activeSessionId, $currentToolPosture, setCurrentToolPosture, type ToolPostureState } from '@/store/session'

// The two reserved virtual presets always exist even without user presets
// (contract §core-semantics-5). Shown first, ahead of any config presets.
const CHAT_ONLY = 'Chat-only'
const FULL = 'Full'
const CUSTOM = 'Custom'

const PILL = cn(
  'h-(--composer-control-size) max-w-52 shrink-0 gap-1 rounded-md px-2 text-xs font-normal',
  'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
)

/**
 * Derive the display label from a live posture. Prefer the backend-stamped
 * `preset` label; fall back to the toolset shape. `enabledToolsets` is `[]` for
 * chat-only (a FALSY value we must test with `.length`, never a truthy coalesce)
 * and `null` for "no override" (full/profile default).
 */
function postureLabel(posture: ToolPostureState | null): string {
  if (posture?.preset) {
    return posture.preset
  }

  if (!posture || posture.enabledToolsets === null) {
    return FULL
  }

  return posture.enabledToolsets.length === 0 ? CHAT_ONLY : CUSTOM
}

/** "7 tools · ~5.4k tok" — a glanceable read on the posture's token footprint. */
function postureBadge(posture: ToolPostureState | null): string {
  if (!posture) {
    return ''
  }

  const parts: string[] = []

  if (typeof posture.toolCount === 'number') {
    parts.push(`${posture.toolCount} ${posture.toolCount === 1 ? 'tool' : 'tools'}`)
  }

  if (typeof posture.toolsEstTokens === 'number') {
    const tok =
      posture.toolsEstTokens >= 1000
        ? `~${(posture.toolsEstTokens / 1000).toFixed(1)}k tok`
        : `~${posture.toolsEstTokens} tok`

    parts.push(tok)
  }

  return parts.join(' · ')
}

function toPosture(session: Record<string, unknown> | undefined): ToolPostureState | null {
  if (!session) {
    return null
  }

  return {
    preset: typeof session.tool_preset === 'string' ? session.tool_preset : null,
    enabledToolsets: Array.isArray(session.enabled_toolsets) ? (session.enabled_toolsets as string[]) : null,
    toolCount: typeof session.tool_count === 'number' ? session.tool_count : null,
    toolsEstTokens: typeof session.tools_est_tokens === 'number' ? session.tools_est_tokens : null
  }
}

/**
 * Per-chat tool-posture selector — a compact composer pill (sibling of the
 * model pill) that shows the live session's tool surface and its token cost, and
 * lets the user swap presets mid-chat. Selecting a preset calls
 * `tools.session_configure`, which is turn-boundary gated: while the session is
 * generating the backend answers `{ok:false, reason:"busy"}`, surfaced here as a
 * non-blocking notice. Only rendered once a session is live (posture is per-chat
 * and has no meaning for a fresh draft).
 */
export function ToolPosturePill({ disabled }: { disabled: boolean }) {
  const posture = useStore($currentToolPosture)
  const activeSessionId = useStore($activeSessionId)
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [presets, setPresets] = useState<ToolPreset[] | null>(null)
  const [pending, setPending] = useState(false)

  const loadPresets = useCallback(() => {
    const gateway = activeGateway()

    if (!gateway) {
      return
    }

    gateway
      .toolsPresetsList()
      .then(result => setPresets(result.presets))
      .catch(() => setPresets([]))
  }, [])

  const applyPreset = useCallback(
    async (preset: string) => {
      const gateway = activeGateway()

      if (!gateway || !activeSessionId || pending) {
        return
      }

      // "Custom…" isn't a preset the backend resolves — it's an affordance that
      // points at the full editor the settings preset manager owns.
      if (preset === CUSTOM) {
        setOpen(false)
        navigate(`${SETTINGS_ROUTE}?tab=tool-presets`)

        return
      }

      setPending(true)

      try {
        const result = await gateway.toolsSessionConfigure({ session_id: activeSessionId, preset })

        if (!result.ok) {
          if (result.reason === 'busy') {
            notify({
              id: 'tool-posture-busy',
              kind: 'info',
              message: 'Finish the current turn first',
              detail: 'Tool changes apply between turns to keep the prompt cache intact.'
            })
          }

          return
        }

        setOpen(false)

        // Prefer the returned session.info snapshot for an instant update; the
        // broadcast session.info event will confirm it a beat later.
        const next = toPosture(result.session)

        if (next) {
          setCurrentToolPosture(next)
        }
      } catch (error) {
        notifyError(error, 'Failed to update tools')
      } finally {
        setPending(false)
      }
    },
    [activeSessionId, navigate, pending]
  )

  // Per-chat posture has no meaning before a session exists — mirror how the
  // model pill is the pickable control for a draft, but tools are not.
  if (!activeSessionId) {
    return null
  }

  const label = postureLabel(posture)
  const badge = postureBadge(posture)

  const builtins = presets?.filter(preset => preset.builtin) ?? [{ name: CHAT_ONLY }, { name: FULL }]
  const userPresets = presets?.filter(preset => !preset.builtin) ?? []
  const title = `Tools: ${label}${badge ? ` (${badge})` : ''}`

  return (
    <DropdownMenu
      onOpenChange={next => {
        setOpen(next)

        if (next) {
          loadPresets()
        }
      }}
      open={open}
    >
      <Tip label={title} side="top">
        <DropdownMenuTrigger asChild>
          <Button aria-label={title} className={PILL} disabled={disabled || pending} type="button" variant="ghost">
            <Wrench className="size-3 shrink-0 opacity-70" />
            <span className="truncate">{label}</span>
            {badge && (
              <Badge className="font-normal" size="xs" variant="muted">
                {badge}
              </Badge>
            )}
            <ChevronDown className="size-2.5 shrink-0 opacity-50" />
          </Button>
        </DropdownMenuTrigger>
      </Tip>
      <DropdownMenuContent align="end" className="w-56" side="top" sideOffset={8}>
        <DropdownMenuLabel>Tool preset</DropdownMenuLabel>
        <DropdownMenuRadioGroup onValueChange={value => void applyPreset(value)} value={label}>
          {builtins.map(preset => (
            <DropdownMenuRadioItem key={preset.name} value={preset.name}>
              {preset.name}
            </DropdownMenuRadioItem>
          ))}
          {userPresets.length > 0 && <DropdownMenuSeparator />}
          {userPresets.map(preset => (
            <DropdownMenuRadioItem key={preset.name} value={preset.name}>
              {preset.name}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={() => {
            setOpen(false)
            navigate(`${SETTINGS_ROUTE}?tab=tool-presets`)
          }}
        >
          Custom…
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
