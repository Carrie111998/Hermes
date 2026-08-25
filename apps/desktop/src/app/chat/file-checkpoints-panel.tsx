import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { getHermesConfigRecord, saveHermesConfig } from '@/hermes'
import { useI18n } from '@/i18n'
import {
  type FileCheckpoint,
  parseFileCheckpointList,
  restoreFileCheckpointParams,
  withCheckpointsEnabled
} from '@/lib/file-checkpoints'
import { confirm } from '@/store/confirm'
import { activeGateway } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { $activeSessionId } from '@/store/session'

interface FileCheckpointsPanelProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  sessionId: string
}

export function FileCheckpointsPanel({ open, onOpenChange, sessionId }: FileCheckpointsPanelProps) {
  const { t } = useI18n()
  const copy = t.assistant.fileCheckpoints
  const [list, setList] = useState(() => parseFileCheckpointList(null))
  const [loading, setLoading] = useState(false)

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setList(parseFileCheckpointList(null))

      return
    }

    const gateway = activeGateway()

    if (!gateway) {
      return
    }

    setLoading(true)

    try {
      const raw = await gateway.request('rollback.list', { session_id: sessionId })
      setList(parseFileCheckpointList(raw))
    } catch (err) {
      notifyError(err, copy.loadFailed)
    } finally {
      setLoading(false)
    }
  }, [copy.loadFailed, sessionId])

  useEffect(() => {
    if (open) {
      void refresh()
    }
  }, [open, refresh])

  const enableCheckpoints = useCallback(async () => {
    const profile = $activeGatewayProfile.get()
    const current = await getHermesConfigRecord(profile)
    await saveHermesConfig(withCheckpointsEnabled(current), profile)
    await refresh()
    notify({ message: copy.enabledToast })
  }, [copy.enabledToast, refresh])

  const revert = useCallback(
    async (row: FileCheckpoint) => {
      const ok = await confirm({
        confirmLabel: copy.revertConfirm,
        description: copy.revertBody,
        destructive: true,
        title: copy.revertTitle
      })

      if (!ok) {
        return
      }

      const gateway = activeGateway()

      if (!gateway) {
        return
      }

      try {
        await gateway.request('rollback.restore', restoreFileCheckpointParams(sessionId, row.hash))
        await refresh()
      } catch (err) {
        notifyError(err, copy.restoreFailed)
      }
    },
    [copy.restoreFailed, copy.revertBody, copy.revertConfirm, copy.revertTitle, refresh, sessionId]
  )

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
        </DialogHeader>
        {!list.enabled ? (
          <div className="flex flex-col gap-3 text-sm text-(--ui-text-secondary)">
            <p>{copy.disabled}</p>
            <Button onClick={() => void enableCheckpoints()} size="sm" type="button">
              {copy.enable}
            </Button>
          </div>
        ) : list.checkpoints.length === 0 ? (
          <p className="text-sm text-(--ui-text-secondary)">{loading ? t.common.loading : copy.empty}</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {list.checkpoints.map((row, index) => (
              <li
                className="flex items-center justify-between gap-3 rounded-md border border-(--ui-border) px-2 py-1.5"
                key={row.hash}
              >
                <div className="min-w-0">
                  <p className="truncate text-sm">{row.message || `#${index + 1}`}</p>
                  <p className="truncate text-xs text-(--ui-text-tertiary)">{row.timestamp || row.hash.slice(0, 8)}</p>
                </div>
                <Button onClick={() => void revert(row)} size="sm" type="button" variant="outline">
                  {copy.revert}
                </Button>
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  )
}

export function FileCheckpointsButton({ sessionId }: { sessionId: string }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const runtimeId = useStore($activeSessionId)

  if (!sessionId && !runtimeId) {
    return null
  }

  return (
    <>
      <Button
        aria-label={t.assistant.fileCheckpoints.menu}
        className="pointer-events-auto"
        onClick={() => setOpen(true)}
        size="icon-titlebar"
        title={t.assistant.fileCheckpoints.menu}
        type="button"
        variant="ghost"
      >
        <Codicon name="history" size="0.875rem" />
      </Button>
      <FileCheckpointsPanel onOpenChange={setOpen} open={open} sessionId={runtimeId || sessionId} />
    </>
  )
}
