import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
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
import { formatRoutingBudget } from '@/lib/turn-routing-budget'
import { $gateway } from '@/store/gateway'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { openModelPicker } from '@/store/session'
import {
  $routingBudget,
  $routingCapability,
  $turnRoutes,
  acceptRoutingBudget,
  acceptRoutingCapability,
  beginRoutingBudgetRequest,
  beginRoutingCapabilityRequest,
  beginRoutingModeWrite,
  rejectRoutingBudget,
  rejectRoutingCapability,
  type RoutingBudgetResponse,
  type RoutingMode
} from '@/store/turn-routing'

interface RoutingModeResponse {
  capability_version?: number
  value?: string
}

const MODE_LABELS: Record<RoutingMode, string> = {
  auto: 'Auto',
  observe: 'Observe',
  off: 'Manual'
}

export function RoutePill({ disabled }: { disabled: boolean }) {
  const view = useSessionView()
  const runtimeId = useStore(view.$runtimeId)
  const gateway = useStore($gateway)
  const profile = normalizeProfileKey(useStore($activeGatewayProfile))
  const budget = useStore($routingBudget)
  const capability = useStore($routingCapability)
  const routes = useStore($turnRoutes)
  const route = runtimeId ? routes[runtimeId] : undefined
  const [open, setOpen] = useState(false)

  const refresh = useCallback(async () => {
    if (!gateway) {
      return
    }

    const generation = beginRoutingCapabilityRequest(profile)
    try {
      const result = await gateway.request<RoutingModeResponse>('config.get', { key: 'routing_mode' })
      acceptRoutingCapability(generation, profile, result.value, result.capability_version)
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error)
      const unavailable = /unknown config key|method not found/i.test(text)
      rejectRoutingCapability(generation, profile, error, unavailable)
    }
  }, [gateway, profile])

  const refreshBudget = useCallback(async () => {
    if (!gateway) {
      return
    }

    const generation = beginRoutingBudgetRequest(profile)
    try {
      const result = await gateway.request<RoutingBudgetResponse>('config.get', { key: 'routing_budget' })
      acceptRoutingBudget(generation, profile, result)
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error)
      const unavailable = /unknown config key|method not found/i.test(text)
      rejectRoutingBudget(generation, profile, error, unavailable)
    }
  }, [gateway, profile])

  useEffect(() => {
    void refresh()
    void refreshBudget()
  }, [refresh, refreshBudget])

  const setMode = useCallback(
    async (value: string) => {
      if (!gateway || (value !== 'off' && value !== 'observe')) {
        return
      }

      const mode = value as RoutingMode
      const write = beginRoutingModeWrite(profile, mode)
      try {
        const result = await gateway.request<RoutingModeResponse>('config.set', { key: 'routing_mode', value: mode })
        acceptRoutingCapability(write.generation, profile, result.value, result.capability_version)
      } catch (error) {
        if (rejectRoutingCapability(write.generation, profile, error, false, write.previous)) {
          notifyError(error, 'Could not change routing mode')
        }
      }
    },
    [gateway, profile]
  )

  // Older backends have no routing control-plane contract. Keep the rest of
  // Desktop fully usable instead of showing a dead control.
  if (capability.profile !== profile) {
    return null
  }
  if (!capability.available && !capability.loading) {
    return null
  }

  const mode = capability.mode
  const label = route?.route ? `${MODE_LABELS[mode]} · ${route.route}` : MODE_LABELS[mode]
  const target = route?.target
  const targetLabel = target?.kind === 'moa' ? target.preset : [target?.provider, target?.model].filter(Boolean).join('/')
  const detail = route
    ? [route.source, route.reasonCode, targetLabel].filter(Boolean).join(' · ')
    : mode === 'observe'
      ? 'Suggestions only; runtime is unchanged'
      : 'Explicit model and session choices only'

  return (
    <DropdownMenu
      onOpenChange={next => {
        setOpen(next)
        if (next) {
          void refresh()
          void refreshBudget()
        }
      }}
      open={open}
    >
      <Tip label={detail || label} side="top">
        <DropdownMenuTrigger asChild>
          <Button
            aria-label={`Routing: ${label}`}
            className="h-(--composer-control-size) max-w-40 shrink-0 rounded-md px-2 text-xs font-normal text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground"
            disabled={disabled || capability.loading}
            type="button"
            variant="ghost"
          >
            <span className="truncate">{label}</span>
          </Button>
        </DropdownMenuTrigger>
      </Tip>
      <DropdownMenuContent align="end" className="w-64" side="top" sideOffset={8}>
        <DropdownMenuLabel>Turn routing</DropdownMenuLabel>
        <DropdownMenuRadioGroup onValueChange={value => void setMode(value)} value={mode}>
          <DropdownMenuRadioItem value="off">Manual</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="observe">Observe</DropdownMenuRadioItem>
          <DropdownMenuRadioItem disabled value="auto">
            Auto (locked until rollout)
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          disabled={!runtimeId}
          onSelect={() => {
            openModelPicker('once')
            setOpen(false)
          }}
        >
          Override next turn…
        </DropdownMenuItem>
        {route ? (
          <>
            <DropdownMenuSeparator />
            <div className="px-2 py-1.5 text-xs text-(--ui-text-tertiary)">
              <div className="font-medium text-foreground">{route.route || 'Current route'}</div>
              <div>{detail}</div>
              {typeof route.confidence === 'number' ? <div>Confidence {Math.round(route.confidence * 100)}%</div> : null}
              {!route.shouldApply && route.mode === 'observe' ? <div>Observed only — not applied</div> : null}
            </div>
          </>
        ) : null}
        {budget.profile === profile && budget.available && budget.status ? (
          <>
            <DropdownMenuSeparator />
            <div className="px-2 py-1.5 text-xs text-(--ui-text-tertiary)">
              <div className="font-medium text-foreground">Budget</div>
              <div>{formatRoutingBudget(budget.status)}</div>
              <div>Week of {budget.status.weekKey} UTC</div>
            </div>
          </>
        ) : null}
        {budget.profile === profile && budget.error && budget.available ? (
          <>
            <DropdownMenuSeparator />
            <div className="px-2 py-1.5 text-xs text-destructive">Budget status: {budget.error}</div>
          </>
        ) : null}
        {capability.error ? (
          <>
            <DropdownMenuSeparator />
            <div className="px-2 py-1.5 text-xs text-destructive">{capability.error}</div>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
