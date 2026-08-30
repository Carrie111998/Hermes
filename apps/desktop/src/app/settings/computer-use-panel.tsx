import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { getActionStatus, getComputerUseStatus, grantComputerUsePermissions, type ProfileScope } from '@/hermes'
import { AlertTriangle, Check, ExternalLink, Loader2, RefreshCw, X } from '@/lib/icons'
import { upsertDesktopActionTask } from '@/store/activity'
import { notify, notifyError } from '@/store/notifications'
import type { ComputerUseStatus } from '@/types/hermes'

import {
  forgetComputerUseGrant,
  peekComputerUseGrant,
  rememberComputerUseGrant
} from './computer-use-grants'
import { Pill } from './primitives'

interface ComputerUsePanelProps {
  /** Re-read the parent toolset list after a permission/install change so the
   *  "Configured / Needs keys" pill stays in sync. */
  onConfiguredChange?: () => void
  profile?: ProfileScope
}

// Per-OS one-liner shown when there's no TCC grant flow (Windows/Linux). macOS
// drives the permission rows instead, so it has no entry here.
const PLATFORM_NOTE: Record<string, string> = {
  linux: 'Drives your desktop via the X11/XWayland accessibility stack — no permission prompt.',
  win32: 'First run may trigger a Windows SmartScreen prompt for the cua-driver UIAccess worker — allow it.'
}

function tone(granted: boolean | null) {
  return granted === true ? 'primary' : 'muted'
}

function GrantIcon({ granted }: { granted: boolean | null }) {
  const Icon = granted === true ? Check : granted === false ? X : AlertTriangle

  return <Icon className="size-3" />
}

function PermissionRow({ granted, label, hint }: { granted: boolean | null; label: string; hint: string }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-background/55 p-2.5">
      <div className="min-w-0">
        <span className="text-sm font-medium">{label}</span>
        <p className="mt-0.5 text-[0.7rem] text-muted-foreground">{hint}</p>
      </div>
      <Pill tone={tone(granted)}>
        <GrantIcon granted={granted} />
        {granted === true ? 'Granted' : granted === false ? 'Not granted' : 'Unknown'}
      </Pill>
    </div>
  )
}

/**
 * Cross-platform Computer Use preflight card.
 *
 * cua-driver runs on macOS, Windows, and Linux, but readiness differs: macOS
 * needs two TCC grants (Accessibility + Screen Recording) that attach to
 * cua-driver's own `com.trycua.driver` identity — not Hermes — and are
 * requested via `cua-driver permissions grant` (dialog attributed to
 * CuaDriver). Windows/Linux have no TCC toggles, so readiness is driver health
 * from `cua-driver doctor`. The backend folds both into one `ready` signal.
 *
 * Binary install/upgrade stays in the cua-driver provider's post-setup runner
 * below this card (the generic ToolsetConfigPanel).
 */
export function ComputerUsePanel({ onConfiguredChange, profile }: ComputerUsePanelProps) {
  const [status, setStatus] = useState<ComputerUseStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [granting, setGranting] = useState(false)
  const activeRef = useRef(false)
  const scopeGenerationRef = useRef(0)
  const refreshRequestRef = useRef(0)

  const refresh = useCallback(async () => {
    const generation = scopeGenerationRef.current
    const request = ++refreshRequestRef.current

    try {
      const nextStatus = await getComputerUseStatus(profile)

      if (activeRef.current && generation === scopeGenerationRef.current && request === refreshRequestRef.current) {
        setStatus(nextStatus)
      }
    } catch (err) {
      if (activeRef.current && generation === scopeGenerationRef.current && request === refreshRequestRef.current) {
        notifyError(err, 'Could not read Computer Use status')
      }
    } finally {
      if (activeRef.current && generation === scopeGenerationRef.current && request === refreshRequestRef.current) {
        setLoading(false)
      }
    }
  }, [profile])

  const pollGrant = useCallback(
    async (name: string, generation: number) => {
      for (
        let attempt = 0;
        attempt < 150 && activeRef.current && generation === scopeGenerationRef.current;
        attempt += 1
      ) {
        await new Promise(resolve => window.setTimeout(resolve, 1500))

        if (!activeRef.current || generation !== scopeGenerationRef.current) {
          return
        }

        const polled = await getActionStatus(name, 200, profile)

        if (!activeRef.current || generation !== scopeGenerationRef.current) {
          return
        }

        upsertDesktopActionTask(polled, profile)

        if (!polled.running) {
          forgetComputerUseGrant(profile)

          return
        }
      }
    },
    [profile]
  )

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    activeRef.current = true
    scopeGenerationRef.current += 1
    const generation = scopeGenerationRef.current
    setStatus(null)
    setLoading(true)
    setGranting(false)
    void refresh()

    const liveName = peekComputerUseGrant(profile)

    if (liveName) {
      setGranting(true)
      void pollGrant(liveName, generation)
        .catch(err => {
          if (activeRef.current && generation === scopeGenerationRef.current) {
            notifyError(err, 'Could not request permissions')
          }
        })
        .finally(() => {
          if (activeRef.current && generation === scopeGenerationRef.current) {
            setGranting(false)
            void refresh()
            onConfiguredChange?.()
          }
        })
    }

    return () => {
      activeRef.current = false
      scopeGenerationRef.current += 1
    }
  }, [onConfiguredChange, pollGrant, profile, refresh])

  // Shared successful post-poll completion for BOTH grant paths in `grant()`
  // (a fresh spawn and an existing-grant retry): once the poll settles, re-read
  // the status card and tell the parent so the "Configured / Needs keys" pill
  // follows the grant. The retry branch previously returned straight after the
  // poll, leaving both stale until the next manual recheck.
  const finishGrantPoll = useCallback(
    async (name: string, generation: number) => {
      await pollGrant(name, generation)

      if (activeRef.current && generation === scopeGenerationRef.current) {
        await refresh()
        onConfiguredChange?.()
      }
    },
    [onConfiguredChange, pollGrant, refresh]
  )

  const grant = useCallback(async () => {
    const generation = scopeGenerationRef.current
    setGranting(true)

    try {
      const existing = peekComputerUseGrant(profile)

      if (existing) {
        await finishGrantPoll(existing, generation)

        return
      }

      const started = await grantComputerUsePermissions(profile)

      if (!started.ok) {
        if (activeRef.current && generation === scopeGenerationRef.current) {
          notifyError(new Error('spawn failed'), 'Could not request permissions')
        }

        return
      }

      rememberComputerUseGrant(profile, started.name)

      if (!activeRef.current || generation !== scopeGenerationRef.current) {
        return
      }

      notify({
        kind: 'info',
        title: 'Approve in System Settings',
        message: 'macOS will show a permission dialog attributed to CuaDriver. Approve it, then return here.'
      })

      await finishGrantPoll(started.name, generation)
    } catch (err) {
      if (activeRef.current && generation === scopeGenerationRef.current) {
        notifyError(err, 'Could not request permissions')
      }
    } finally {
      if (activeRef.current && generation === scopeGenerationRef.current) {
        setGranting(false)
      }
    }
  }, [finishGrantPoll, profile])

  if (loading) {
    return (
      <div className="flex items-center gap-2 px-1 text-xs text-muted-foreground">
        <Loader2 className="size-3.5 animate-spin" />
        Checking Computer Use status…
      </div>
    )
  }

  if (!status) {
    return null
  }

  if (!status.platform_supported) {
    return (
      <p className="px-1 text-xs text-muted-foreground">
        Computer Use isn&apos;t supported on this platform ({status.platform}).
      </p>
    )
  }

  if (!status.installed) {
    return (
      <p className="px-1 text-xs text-muted-foreground">
        Install the cua-driver backend below to drive this machine.
        {status.can_grant && ' Then grant Accessibility and Screen Recording here.'}
      </p>
    )
  }

  const failingChecks = status.checks.filter(c => c.status !== 'ok')

  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2 px-1">
        <div className="min-w-0">
          {status.can_grant ? (
            <p className="text-[0.72rem] text-muted-foreground">
              Grants attach to CuaDriver&apos;s own identity (com.trycua.driver), not Hermes — so the dialog is
              attributed to the process that drives your Mac.
            </p>
          ) : (
            <p className="text-[0.72rem] text-muted-foreground">{PLATFORM_NOTE[status.platform] ?? ''}</p>
          )}
          {status.version && <p className="text-[0.68rem] text-muted-foreground/80">{status.version}</p>}
        </div>
        <Button onClick={() => void refresh()} size="sm" variant="text">
          <RefreshCw className="size-3.5" />
          Recheck
        </Button>
      </div>

      {status.can_grant ? (
        <>
          <PermissionRow
            granted={status.accessibility}
            hint="Lets cua-driver post clicks, keystrokes, and read the accessibility tree."
            label="Accessibility"
          />
          <PermissionRow
            granted={status.screen_recording}
            hint="Lets cua-driver capture screenshots of app windows."
            label="Screen Recording"
          />
        </>
      ) : (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-background/55 p-2.5">
          <span className="text-sm font-medium">Driver health</span>
          <Pill tone={tone(status.ready)}>
            <GrantIcon granted={status.ready} />
            {status.ready === true ? 'Ready' : status.ready === false ? 'Not ready' : 'Unknown'}
          </Pill>
        </div>
      )}

      {failingChecks.map(c => (
        <p className="px-1 text-[0.7rem] text-muted-foreground" key={c.label}>
          <AlertTriangle className="mr-1 inline size-3" />
          {c.label}: {c.message}
        </p>
      ))}

      {status.error && (
        <p className="px-1 text-[0.7rem] text-muted-foreground">
          <AlertTriangle className="mr-1 inline size-3" />
          {status.error}
        </p>
      )}

      {status.ready ? (
        <div className="flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
          <Check className="size-3.5" />
          Computer Use is ready. Ask the agent to capture an app and click around.
        </div>
      ) : (
        status.can_grant && (
          <Button disabled={granting} onClick={() => void grant()} size="sm">
            {granting ? <Loader2 className="size-3.5 animate-spin" /> : <ExternalLink className="size-3.5" />}
            {granting ? 'Waiting for approval…' : 'Grant permissions'}
          </Button>
        )
      )}
    </div>
  )
}
