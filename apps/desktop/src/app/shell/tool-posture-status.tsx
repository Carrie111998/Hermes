import type { ToolPreset } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router'

import { SETTINGS_ROUTE } from '@/app/routes'
import { STATUSBAR_ACTION_CLASS } from '@/app/shell/statusbar-controls'
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
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { Wrench } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { activeGateway } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import {
  $activeSessionId,
  $currentToolPosture,
  $newChatToolPreset,
  setCurrentToolPosture,
  setNewChatToolPreset,
  type ToolPostureState
} from '@/store/session'

// The two reserved virtual presets always exist even without user presets. Shown
// first, ahead of any config presets.
const CHAT_ONLY = 'Chat-only'
const FULL = 'Full'
const CUSTOM = 'Custom'

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

/** "19 · ~10.1k" — the live tool-schema footprint, or null when unknown. */
function postureFootprint(posture: ToolPostureState | null): string | null {
  if (!posture || typeof posture.toolsEstTokens !== 'number') {
    return null
  }

  const tokens =
    posture.toolsEstTokens >= 1000 ? `~${(posture.toolsEstTokens / 1000).toFixed(1)}k` : `~${posture.toolsEstTokens}`

  return typeof posture.toolCount === 'number' ? `${posture.toolCount} · ${tokens}` : tokens
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
 * Per-chat tool-posture selector, living in the bottom status bar next to the
 * context-usage / session-timer / approval-mode readouts. Shows the active
 * chat's preset + its tool-schema footprint and, on click, opens the preset
 * picker (the affordance that used to be a composer pill).
 *
 * Two modes, mirroring the old pill:
 *   - **Live session:** shows the session's tool surface + token cost. Selecting
 *     a preset calls `tools.session_configure`, turn-boundary gated (a `busy`
 *     answer surfaces as a non-blocking notice).
 *   - **Draft (no live session):** shows the pick the NEXT new chat starts with
 *     (sticky draft pick → configured default → Full). Selecting stores the
 *     sticky pre-session pick that rides on the next `session.create`.
 */
export function ToolPostureStatus() {
  const { t } = useI18n()
  const title = t.shell.statusbar.openToolPresets
  const posture = useStore($currentToolPosture)
  const activeSessionId = useStore($activeSessionId)
  const draftPreset = useStore($newChatToolPreset)
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [presets, setPresets] = useState<ToolPreset[] | null>(null)
  const [pending, setPending] = useState(false)
  const [defaultPreset, setDefaultPreset] = useState<string | null>(null)

  const isDraft = !activeSessionId

  // In draft mode the label follows the configured default when no sticky draft
  // pick exists, so fetch it up front to render the right "starts as" preset.
  useEffect(() => {
    if (!isDraft) {
      return
    }

    const gateway = activeGateway()

    if (!gateway) {
      return
    }

    let cancelled = false

    gateway
      .toolsDefaultPresetGet()
      .then(result => {
        if (!cancelled) {
          setDefaultPreset(result.name)
        }
      })
      .catch((err: unknown) => {
        // Distinguish a real fetch failure (connectivity / backend) from a
        // genuinely-unset default; both fall back to null but only one is a bug.
        console.warn('ToolPostureStatus: failed to fetch default preset', err)
        if (!cancelled) {
          setDefaultPreset(null)
        }
      })

    return () => {
      cancelled = true
    }
  }, [isDraft])

  const loadPresets = useCallback(() => {
    const gateway = activeGateway()

    if (!gateway) {
      return
    }

    gateway
      .toolsPresetsList()
      .then(result => setPresets(result.presets))
      .catch((err: unknown) => {
        // Log so an empty dropdown from a gateway error is distinguishable from
        // a genuinely-empty preset list.
        console.warn('ToolPostureStatus: failed to load presets', err)
        setPresets([])
      })
  }, [])

  const applyPreset = useCallback(
    async (preset: string) => {
      // "Custom…" isn't a preset the backend resolves — it points at the full
      // editor the settings preset manager owns.
      if (preset === CUSTOM) {
        setOpen(false)
        navigate(`${SETTINGS_ROUTE}?tab=tool-presets`)

        return
      }

      // Draft mode: store the sticky pre-session pick. It rides on the next
      // session.create (as `tool_preset`) and overrides the configured default.
      if (isDraft) {
        setNewChatToolPreset(preset)
        setOpen(false)

        return
      }

      const gateway = activeGateway()

      if (!gateway || !activeSessionId || pending) {
        return
      }

      setPending(true)

      try {
        // Request the loose record shape: `result.session` is fed to
        // `toPosture`, which parses the same untyped `session.info` broadcast.
        const result = await gateway.toolsSessionConfigure<Record<string, unknown>>({
          session_id: activeSessionId,
          preset
        })

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
    [activeSessionId, isDraft, navigate, pending]
  )

  // Draft: the label reflects what the next chat starts with (sticky pick →
  // configured default → Full). Live: derive from the session's posture; the
  // footprint detail only has meaning for a live surface.
  const label = isDraft ? (draftPreset ?? defaultPreset ?? FULL) : postureLabel(posture)
  const detail = isDraft ? null : postureFootprint(posture)

  const builtins = presets?.filter(preset => preset.builtin) ?? [{ name: CHAT_ONLY }, { name: FULL }]
  const userPresets = presets?.filter(preset => !preset.builtin) ?? []

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
      <TooltipProvider delayDuration={0}>
        <Tooltip>
          <TooltipTrigger asChild>
            <DropdownMenuTrigger asChild>
              <button className={cn(STATUSBAR_ACTION_CLASS)} disabled={pending} type="button">
                <Wrench className="size-3" />
                <span className="truncate">{label}</span>
                {detail && <span className="truncate text-muted-foreground/80">{detail}</span>}
              </button>
            </DropdownMenuTrigger>
          </TooltipTrigger>
          <TooltipContent>{title}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
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
