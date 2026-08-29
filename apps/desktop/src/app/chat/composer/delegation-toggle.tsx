import { useState } from 'react'

import { getNested, setNested } from '@/app/settings/helpers'
import { Button } from '@/components/ui/button'
import { Tip } from '@/components/ui/tooltip'
import { saveHermesConfig } from '@/hermes'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { GitBranch, iconSize, Loader2 } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'

import {
  invalidateHermesConfig,
  setHermesConfigCache,
  useHermesConfigRecord
} from '../../hooks/use-config-record'

import { ACTIVE_ICON_BTN, GHOST_ICON_BTN } from './control-classes'

export const ROUTE_REPO_CHANGES_KEY = 'delegate_wave.route_repo_changes'

/**
 * Delegate-wave routing, on the composer row rather than four clicks into
 * Settings ▸ Safety.
 *
 * This is a POLICY switch, not a per-message one. It writes the same
 * `delegate_wave.route_repo_changes` key the settings field wrote, so the
 * backend sees exactly what it saw before and no new contract exists. What
 * changes is only where you reach it: the decision "should Hermes edit this
 * repository itself" belongs next to the message that is about to ask it to.
 *
 * ABSENT MEANS OFF, but UNREADABLE MEANS NEITHER. The backend defaults this key
 * by absence (`routing_enabled` in `tools/delegate_routing.py`), so an unset
 * config renders an off switch rather than an empty one. A config it cannot
 * read at all is a different case: the backend raises there rather than assume
 * off, and this component likewise renders nothing rather than assert a state
 * it does not have.
 */
export function DelegationToggle({ disabled }: { disabled: boolean }) {
  const { t } = useI18n()
  const c = t.composer
  const { data: config } = useHermesConfigRecord()
  const [saving, setSaving] = useState(false)

  // No config yet (gateway still starting, or a failed fetch) means we cannot
  // honestly show a state, and a switch that lies about a safety policy is
  // worse than one that is briefly absent.
  if (!config) {
    return null
  }

  const active = getNested(config, ROUTE_REPO_CHANGES_KEY) === true
  const label = active ? c.delegationRoutingOn : c.delegationRoutingOff

  const toggle = async () => {
    if (saving) {
      return
    }

    const next = setNested(config, ROUTE_REPO_CHANGES_KEY, !active)

    setSaving(true)
    // Optimistic: the switch answers the click immediately, and a failed save
    // rolls the cache back by refetching rather than leaving a switch that
    // claims a policy the backend never accepted.
    setHermesConfigCache(next)

    try {
      const result = await saveHermesConfig(next)

      if (!result.ok) {
        throw new Error(c.delegationRoutingSaveFailed)
      }
    } catch (error) {
      // Roll back immediately. Invalidation may fail for the same reason as
      // the save and cannot be the only correction for a safety switch that
      // never persisted.
      setHermesConfigCache(config)
      notifyError(error, c.delegationRoutingSaveFailed)
    } finally {
      void invalidateHermesConfig()
      setSaving(false)
    }
  }

  return (
    <Tip label={label}>
      <Button
        aria-label={label}
        aria-pressed={active}
        className={cn(GHOST_ICON_BTN, 'p-0', active && ACTIVE_ICON_BTN)}
        disabled={disabled || saving}
        onClick={() => {
          triggerHaptic(active ? 'close' : 'open')
          void toggle()
        }}
        size="icon"
        type="button"
        variant="ghost"
      >
        {saving ? (
          <Loader2 className={cn(iconSize.sm, 'animate-spin')} />
        ) : (
          <GitBranch className={cn(iconSize.sm, !active && 'opacity-60')} />
        )}
      </Button>
    </Tip>
  )
}
