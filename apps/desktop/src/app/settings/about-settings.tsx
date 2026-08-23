import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { BrandMark } from '@/components/brand-mark'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import type { DesktopUpdateStatus } from '@/global'
import { type Translations, useI18n } from '@/i18n'
import { AlertTriangle, CheckCircle2, ExternalLink, Loader2, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { $connection } from '@/store/session'
import {
  $backendUpdateApply,
  $backendUpdateChecking,
  $backendUpdateStatus,
  $desktopVersion,
  $updateApply,
  $updateChecking,
  $updateStatus,
  checkBackendUpdates,
  checkUpdates,
  openUpdateOverlayFor,
  refreshDesktopVersion,
  restartBackendGatewayForSkew,
  startUpdateFor,
  type UpdateApplyState,
  type UpdateTarget
} from '@/store/updates'

import { ListRow, SectionHeading, SettingsContent } from './primitives'
import { UninstallSection } from './uninstall-section'

const RELEASE_NOTES_URL = 'https://github.com/NousResearch/hermes-agent/releases'
const INSTALLER_URL = 'https://hermes-agent.nousresearch.com/'

function relativeTime(ms: number | undefined, a: Translations['settings']['about']) {
  if (!ms) {
    return a.never
  }

  const diff = Date.now() - ms

  if (diff < 60_000) {
    return a.justNow
  }

  if (diff < 3_600_000) {
    return a.minAgo(Math.round(diff / 60_000))
  }

  if (diff < 86_400_000) {
    return a.hoursAgo(Math.round(diff / 3_600_000))
  }

  return a.daysAgo(Math.round(diff / 86_400_000))
}

function updatePresentation(
  status: DesktopUpdateStatus | null,
  apply: UpdateApplyState,
  a: Translations['settings']['about']
) {
  const behind = status?.behind ?? 0
  const updateAvailable = behind > 0 || Boolean(status?.updateAvailable)
  const supported = status?.supported !== false
  const applying = apply.applying || apply.stage === 'restart'
  const restartFailed = apply.error === 'gateway-restart-failed'
  const restartRequired = status?.gatewayRestartRequired === true
  let statusLine: string
  let statusTone: 'idle' | 'available' | 'error' = 'idle'

  if (restartFailed) {
    statusLine = apply.message || a.cantReach
    statusTone = 'error'
  } else if (applying) {
    statusLine = apply.stage === 'restart' && apply.message ? apply.message : a.installing
    statusTone = 'available'
  } else if (restartRequired) {
    statusLine = a.gatewayRestartRequired
    statusTone = 'available'
  } else if (!supported) {
    statusLine = status?.message ?? a.cantUpdate
    statusTone = 'error'
  } else if (status?.error) {
    statusLine = a.cantReach
    statusTone = 'error'
  } else if (updateAvailable) {
    statusLine = behind > 0 ? a.updateReady(behind) : a.updateReadyUnknown
    statusTone = 'available'
  } else if (status) {
    statusLine = a.onLatest
  } else {
    statusLine = a.tapCheck
  }

  return { applying, behind, restartRequired, statusLine, statusTone, supported, updateAvailable }
}

function UpdateStatusCard({
  a,
  apply,
  checking,
  justChecked,
  label,
  onCheck,
  status,
  target
}: {
  a: Translations['settings']['about']
  apply: UpdateApplyState
  checking: boolean
  justChecked: boolean
  label: string
  onCheck: () => void
  status: DesktopUpdateStatus | null
  target: UpdateTarget
}) {
  const { applying, restartRequired, statusLine, statusTone, supported, updateAvailable } = updatePresentation(
    status,
    apply,
    a
  )

  return (
    <div
      className={cn(
        'rounded-xl border px-4 py-3 text-sm',
        statusTone === 'available' && 'border-primary/30 bg-primary/5 text-foreground',
        statusTone === 'error' && 'border-destructive/35 bg-destructive/5 text-destructive',
        statusTone === 'idle' && 'border-border/70 bg-muted/20 text-foreground'
      )}
      data-update-target={target}
    >
      <div className="flex items-start gap-2">
        {statusTone === 'available' ? (
          <Codicon className="mt-0.5 size-4 shrink-0 text-primary" name="cloud-download" size="1rem" />
        ) : statusTone === 'error' ? null : (
          <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
        )}
        <div className="min-w-0">
          <p className="font-medium">{label}</p>
          <p className="mt-0.5">{statusLine}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {a.lastChecked(relativeTime(status?.fetchedAt, a))}
            {justChecked && !checking ? a.justNowSuffix : ''}
          </p>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-4">
        <Button disabled={checking || applying || !supported} onClick={onCheck} size="sm" variant="textStrong">
          {checking ? <Loader2 className="size-3 animate-spin" /> : <RefreshCw className="size-3" />}
          {checking ? a.checking : a.checkNow}
        </Button>

        {target === 'backend' && restartRequired && !applying && (
          <Button onClick={() => void restartBackendGatewayForSkew()} size="sm">
            <RefreshCw className="size-3" />
            {a.restartGateway}
          </Button>
        )}

        {updateAvailable && supported && !applying && (
          <>
            <Button onClick={() => startUpdateFor(target)} size="sm">
              {a.updateNow}
            </Button>
            <Button onClick={() => openUpdateOverlayFor(target)} size="sm" variant="textStrong">
              {a.seeWhatsNew}
            </Button>
          </>
        )}
      </div>
    </div>
  )
}

export function AboutSettings() {
  const { t } = useI18n()
  const a = t.settings.about
  const version = useStore($desktopVersion)
  const connection = useStore($connection)
  const clientStatus = useStore($updateStatus)
  const clientApply = useStore($updateApply)
  const clientChecking = useStore($updateChecking)
  const backendStatus = useStore($backendUpdateStatus)
  const backendApply = useStore($backendUpdateApply)
  const backendChecking = useStore($backendUpdateChecking)
  const [justChecked, setJustChecked] = useState<UpdateTarget | null>(null)
  const showBackend = connection?.mode === 'remote'

  // The version atom is loaded once at app boot, which makes About show a
  // stale number after a self-update (the running binary is current, the
  // displayed string is not). Re-read on mount so opening About always
  // reflects the running build.
  useEffect(() => {
    void refreshDesktopVersion()
  }, [])

  const handleCheck = async (target: UpdateTarget) => {
    setJustChecked(null)
    const next = await (target === 'backend' ? checkBackendUpdates() : checkUpdates())

    if (next) {
      setJustChecked(target)
    }
  }

  return (
    <SettingsContent>
      <div className="flex flex-col items-center gap-3 pt-6 pb-2 text-center">
        <BrandMark className="size-16" />
        <div>
          <h2 className="text-lg font-semibold tracking-tight">{a.heading}</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {version?.appVersion ? a.version(version.appVersion) : a.versionUnavailable}
          </p>
        </div>
        {version?.bundleOutOfSync && (
          <div className="mx-auto w-full max-w-2xl rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-left text-sm">
            <div className="flex items-start gap-2">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
              <div className="min-w-0">
                <p className="font-medium">{a.bundleOutOfSync}</p>
                <p className="mt-1 text-xs text-muted-foreground">{a.bundleOutOfSyncDesc}</p>
                <Button asChild className="mt-2" size="sm" variant="textStrong">
                  <a
                    href={INSTALLER_URL}
                    onClick={event => {
                      event.preventDefault()
                      void window.hermesDesktop?.openExternal?.(INSTALLER_URL)
                    }}
                    rel="noreferrer"
                    target="_blank"
                  >
                    <ExternalLink className="size-3" />
                    {a.bundleOutOfSyncAction}
                  </a>
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>

      <div className="mx-auto mt-4 w-full max-w-2xl">
        <SectionHeading icon={RefreshCw} title={a.updates} />

        <div className="grid gap-3">
          <UpdateStatusCard
            a={a}
            apply={clientApply}
            checking={clientChecking}
            justChecked={justChecked === 'client'}
            label={a.clientUpdates}
            onCheck={() => void handleCheck('client')}
            status={clientStatus}
            target="client"
          />

          {showBackend && (
            <UpdateStatusCard
              a={a}
              apply={backendApply}
              checking={backendChecking}
              justChecked={justChecked === 'backend'}
              label={a.backendUpdates}
              onCheck={() => void handleCheck('backend')}
              status={backendStatus}
              target="backend"
            />
          )}

          <Button asChild className="ml-auto" size="sm" variant="text">
            <a
              href={RELEASE_NOTES_URL}
              onClick={event => {
                event.preventDefault()
                void window.hermesDesktop?.openExternal?.(RELEASE_NOTES_URL)
              }}
              rel="noreferrer"
              target="_blank"
            >
              <ExternalLink className="size-3" />
              {a.releaseNotes}
            </a>
          </Button>
        </div>

        <ListRow
          description={a.automaticUpdatesDesc}
          hint={a.branchCommit(clientStatus?.branch ?? 'unknown', clientStatus?.currentSha?.slice(0, 7) ?? 'unknown')}
          title={a.automaticUpdates}
        />

        <UninstallSection />
      </div>
    </SettingsContent>
  )
}
