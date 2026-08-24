import { useStore } from '@nanostores/react'
import { useEffect, useMemo } from 'react'

import { Button } from '@/components/ui/button'
import { CopyButton } from '@/components/ui/copy-button'
import { useI18n } from '@/i18n'
import { Box, Cloud, Loader2, RefreshCw } from '@/lib/icons'
import { $connectionsRegistry } from '@/store/connections'
import {
  $fleetUpdates,
  $fleetUpdatesRefreshing,
  applyFleetUpdate,
  type FleetUpdateOutcome,
  type FleetUpdateRow,
  refreshFleetConnection,
  refreshFleetUpdates
} from '@/store/fleet-updates'
import { notify, notifyError } from '@/store/notifications'
import { $updateEverything, applyEverythingUpdate } from '@/store/updates'

import { ListRow, Pill, SectionHeading } from './primitives'

function outcomeTone(outcome: FleetUpdateOutcome): 'muted' | 'primary' | 'warn' {
  if (outcome === 'available' || outcome === 'running') {
    return 'primary'
  }

  if (outcome === 'failed' || outcome === 'manual' || outcome === 'partial') {
    return 'warn'
  }

  return 'muted'
}

export function FleetUpdatesSection() {
  const { t } = useI18n()
  const copy = t.settings.fleetUpdates
  const registry = useStore($connectionsRegistry)
  const registryLoaded = registry !== null
  const rows = useStore($fleetUpdates)
  const refreshing = useStore($fleetUpdatesRefreshing)
  const everything = useStore($updateEverything)
  const connectionsKey = useMemo(
    () =>
      (registry?.connections ?? [])
        .filter(connection => connection.kind !== 'local')
        .map(connection => `${connection.id}:${connection.kind}:${connection.installId ?? ''}:${connection.label}`)
        .join('|'),
    [registry?.connections]
  )

  useEffect(() => {
    if (registryLoaded) {
      void refreshFleetUpdates()
    }
  }, [connectionsKey, registryLoaded])

  const fleetRows = Object.values(rows)

  if (!connectionsKey && fleetRows.length === 0) {
    return null
  }

  const statusLabel = (row: FleetUpdateRow) => {
    switch (row.outcome) {
      case 'available':
        return copy.available
      case 'checking':
        return copy.checking
      case 'current':
        return copy.current
      case 'failed':
        return copy.failed
      case 'managed':
        return row.deploymentKind === 'cloud' ? copy.cloudManaged : copy.deploymentKinds.external
      case 'manual':
        return copy.manual
      case 'partial':
        return copy.partial
      case 'running':
        return copy.applying
      case 'restart-required':
        return copy.restartRequired
      case 'restarted':
        return copy.restarted
      case 'success':
        return copy.updated
      default:
        return copy.notChecked
    }
  }

  const apply = async (row: FleetUpdateRow) => {
    try {
      const result = await applyFleetUpdate(row.connectionId)

      if (result.outcome === 'failed') {
        notifyError(new Error(result.message || copy.failed), copy.backendTitle(row.label))
      } else {
        notify({
          message:
            result.outcome === 'manual'
              ? copy.manualToast
              : result.outcome === 'partial'
                ? copy.partialToast
                : result.outcome === 'managed'
                  ? copy.cloudManaged
                  : result.outcome === 'restarted'
                    ? copy.restarted
                    : result.outcome === 'current'
                      ? copy.current
                      : copy.updated,
          title: copy.backendTitle(row.label)
        })
      }
    } catch (error) {
      notifyError(error, copy.backendTitle(row.label))
    }
  }

  const action = (row: FleetUpdateRow) => {
    if (row.outcome === 'checking' || row.outcome === 'running') {
      return (
        <Button disabled size="sm" variant="secondary">
          <Loader2 className="animate-spin" /> {row.outcome === 'running' ? copy.applying : copy.checking}
        </Button>
      )
    }

    if (row.action === 'managed') {
      return (
        <Button disabled size="sm" variant="secondary">
          <Cloud /> {row.deploymentKind === 'cloud' ? copy.cloudManaged : copy.deploymentKinds.external}
        </Button>
      )
    }

    if (row.action === 'manual' && row.updateCommand) {
      return <CopyButton buttonSize="sm" buttonVariant="secondary" label={copy.copyCommand} text={row.updateCommand} />
    }

    if (row.action === 'retry') {
      return (
        <Button onClick={() => void refreshFleetConnection(row.connectionId)} size="sm" variant="secondary">
          <RefreshCw /> {copy.retry}
        </Button>
      )
    }

    if (row.action === 'restart') {
      return (
        <Button onClick={() => void apply(row)} size="sm" variant="secondary">
          <RefreshCw /> {copy.restartGateway}
        </Button>
      )
    }

    if (row.action === 'apply') {
      return (
        <Button onClick={() => void apply(row)} size="sm">
          {copy.applyBackend}
        </Button>
      )
    }

    return <Pill tone={outcomeTone(row.outcome)}>{statusLabel(row)}</Pill>
  }

  return (
    <section className="mt-8">
      <SectionHeading
        aside={
          <div className="flex items-center gap-2">
            <Button
              disabled={refreshing || everything.running}
              onClick={() => void refreshFleetUpdates({ force: true, includeInactiveSsh: true })}
              size="xs"
              variant="text"
            >
              {refreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              {copy.refresh}
            </Button>
            <Button
              disabled={refreshing || everything.running}
              onClick={() => void applyEverythingUpdate()}
              size="xs"
              variant="textStrong"
            >
              {everything.running ? <Loader2 className="animate-spin" /> : null}
              {everything.running ? copy.updatingAll : copy.updateAll}
            </Button>
          </div>
        }
        icon={Box}
        title={copy.title}
      />
      <p className="mb-1 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {copy.intro}
      </p>

      <div className="grid gap-1">
        {fleetRows.map(row => {
          const detail = [
            copy.backendVersion(row.currentVersion ?? copy.unknown),
            copy.deployment(copy.deploymentKinds[row.deploymentKind]),
            statusLabel(row)
          ].join(' · ')
          const botContext =
            row.botProfiles?.length || row.botPlatforms?.length
              ? copy.botContext(row.botProfiles ?? [], row.botPlatforms ?? [])
              : null

          return (
            <ListRow
              action={action(row)}
              below={
                row.error || row.message || botContext ? (
                  <div className="mt-1 grid gap-1 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                    {row.error || row.message ? <p>{row.error || row.message}</p> : null}
                    {botContext ? <p>{botContext}</p> : null}
                  </div>
                ) : null
              }
              description={detail}
              hint={row.updateCommand ?? undefined}
              key={row.connectionId}
              title={
                <span className="flex items-center gap-2">
                  <span>{row.label}</span>
                  <Pill tone={outcomeTone(row.outcome)}>{statusLabel(row)}</Pill>
                </span>
              }
            />
          )
        })}
      </div>
    </section>
  )
}
