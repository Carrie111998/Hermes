/**
 * Hermes Office (Claw3d) — status, install/update, start/stop, logs and
 * browser-open for the hermes-office 3D interface, driven by the Electron
 * main-process manager (electron/claw3d.ts) through window.hermesDesktop.
 */
import {
  Badge,
  Button,
  cn,
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  ScrollArea,
  StatusDot,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  $setupProgress,
  getLogs,
  getStatus,
  type OfficeStatus,
  openOffice,
  runSetup,
  startOffice,
  stopOffice
} from './api'
import { useOfficeI18n } from './i18n'

function StatusRow({ label, value, ok }: { label: string; value: string; ok?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-2 text-sm">
      <span className="text-(--ui-text-secondary)">{label}</span>
      <span
        className={cn(
          'flex items-center gap-1.5',
          ok === false ? 'text-(--ui-danger,#f87171)' : 'text-(--ui-text-primary)'
        )}
      >
        {value}
      </span>
    </div>
  )
}

export function OfficeScreen() {
  const k = useOfficeI18n()
  const progress = useValue($setupProgress)
  const [showLogs, setShowLogs] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const { data: status, refetch } = useQuery({
    queryKey: ['office', 'status'],
    queryFn: () => getStatus(),
    refetchInterval: 5_000
  })

  const { data: logs } = useQuery({
    queryKey: ['office', 'logs'],
    queryFn: () => getLogs(),
    refetchInterval: 5_000,
    enabled: showLogs
  })

  const s: OfficeStatus | undefined = status

  const doSetup = async () => {
    setActionError(null)

    try {
      const result = await runSetup()

      if (!result.ok) {setActionError(result.error ?? k.setupFailed)}
      await refetch()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : k.setupFailed)
    }
  }

  const doStart = async () => {
    setActionError(null)
    const result = await startOffice()

    if (!result.success) {setActionError(result.error ?? k.startFailed)}
    await refetch()
  }

  const doStop = async () => {
    setActionError(null)

    try {
      await stopOffice()
      await refetch()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : k.stopFailed)
    }
  }

  const doOpen = async () => {
    setActionError(null)
    const result = await openOffice()

    if (!result.ok) {setActionError(result.error ?? k.openBrowser)}
  }

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-(--ui-text-primary)">{k.title}</h1>
          <p className="text-xs text-(--ui-text-secondary)">{k.subtitle}</p>
        </div>
        <Button onClick={() => void refetch()} size="sm" variant="ghost">
          {k.refresh}
        </Button>
      </div>

      {s?.oauthUnsupported && (
        <div className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) p-3 text-sm text-(--ui-text-secondary)">
          {k.oauthUnsupportedBody}
        </div>
      )}

      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
        <div className="flex flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) p-3">
          <div className="flex items-center gap-2">
            <StatusDot className={cn(s?.running && 'animate-pulse')} tone={s?.running ? 'good' : 'muted'} />
            <span className="text-sm font-medium text-(--ui-text-primary)">{s?.running ? k.running : k.stopped}</span>
            <Badge className="ml-auto" variant="outline">
              {s?.cloned ? (s?.installed ? 'installed' : k.notInstalled) : k.notCloned}
            </Badge>
          </div>
          <StatusRow
            label={k.devServer}
            ok={s ? s.devServerRunning : undefined}
            value={s?.devServerRunning ? k.running : k.stopped}
          />
          <StatusRow
            label={k.adapter}
            ok={s ? s.adapterRunning : undefined}
            value={s?.adapterRunning ? k.running : k.stopped}
          />
          <StatusRow label={k.port} value={s ? String(s.port) : '—'} />
          <StatusRow label={k.gateway} value={s?.url ?? '—'} />
          {s?.portInUse && <StatusRow label={k.portInUse} ok={false} value="⚠" />}
          <StatusRow label={k.error} ok={s ? !s.error : undefined} value={s?.error || k.noError} />
        </div>

        <div className="flex flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) p-3">
          <span className="text-sm font-medium text-(--ui-text-primary)">{k.actions}</span>
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={!s?.cloned || Boolean(progress)}
              onClick={() => void doSetup()}
              size="sm"
              variant="outline"
            >
              {k.setup}
            </Button>
            <Button
              disabled={!s?.installed || s?.running || Boolean(progress)}
              onClick={() => void doStart()}
              size="sm"
            >
              {k.start}
            </Button>
            <Button disabled={!s?.running} onClick={() => void doStop()} size="sm" variant="outline">
              {k.stop}
            </Button>
            <Button disabled={!s?.running} onClick={() => void doOpen()} size="sm" variant="outline">
              {k.openBrowser}
            </Button>
            <Button onClick={() => setShowLogs(v => !v)} size="sm" variant="ghost">
              {k.logs}
            </Button>
          </div>
          {actionError && <p className="text-xs text-(--ui-danger,#f87171)">{actionError}</p>}
          {s?.oauthUnsupported && <p className="text-xs text-(--ui-text-tertiary)">{k.oauthUnsupportedBody}</p>}
        </div>
      </div>

      {showLogs && (
        <div className="flex min-h-0 flex-1 flex-col rounded-lg border border-(--ui-stroke-secondary)">
          <div className="border-b border-(--ui-stroke-secondary) px-3 py-2 text-sm font-medium text-(--ui-text-primary)">
            {k.logs}
          </div>
          <ScrollArea className="min-h-0 flex-1">
            <pre className="whitespace-pre-wrap p-3 font-mono text-xs leading-relaxed text-(--ui-text-secondary)">
              {logs?.logs || k.noLogs}
            </pre>
          </ScrollArea>
        </div>
      )}

      <Dialog onOpenChange={() => undefined} open={Boolean(progress)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{k.setupTitle}</DialogTitle>
          </DialogHeader>
          {progress && (
            <div className="flex flex-col gap-2">
              <span className="text-sm text-(--ui-text-primary)">
                {k.step(progress.step, progress.totalSteps, progress.title)}
              </span>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-(--ui-stroke-secondary)">
                <div
                  className="h-full bg-(--ui-accent) transition-all"
                  style={{ width: `${(progress.step / Math.max(1, progress.totalSteps)) * 100}%` }}
                />
              </div>
              <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md bg-(--ui-surface) p-2 font-mono text-[0.6875rem] text-(--ui-text-secondary)">
                {progress.log}
              </pre>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
