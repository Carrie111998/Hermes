import { useCallback, useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { getLinkedDevices, revokeLinkedDevice } from '@/hermes'
import { useI18n } from '@/i18n'
import { Monitor } from '@/lib/icons'
import { notify, notifyError } from '@/store/notifications'
import type { LinkedDevice } from '@/types/hermes'

import { ListRow, SettingsSection } from './primitives'

export function LinkedDevicesSettings() {
  const { t } = useI18n()
  const copy = t.settings.gateway
  const [devices, setDevices] = useState<LinkedDevice[] | null>(null)
  const [loadFailed, setLoadFailed] = useState(false)
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  const [revokingId, setRevokingId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoadFailed(false)
    setDevices(null)

    try {
      const result = await getLinkedDevices()
      setDevices(result.devices)
    } catch {
      setLoadFailed(true)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const uniqueDevices = useMemo(
    () => Array.from(new Map(devices?.map(device => [device.id, device])).values()),
    [devices]
  )

  const revoke = async (device: LinkedDevice) => {
    setRevokingId(device.id)

    try {
      await revokeLinkedDevice(device.id)
      setDevices(current => current?.filter(item => item.id !== device.id) ?? current)
      setConfirmingId(null)
      notify({ kind: 'success', message: copy.linkedDevicesRevoked })
    } catch (error) {
      notifyError(error, copy.linkedDevicesRevokeFailed)
    } finally {
      setRevokingId(null)
    }
  }

  return (
    <SettingsSection icon={Monitor} title={copy.linkedDevicesTitle}>
      {devices === null && !loadFailed ? (
        <div aria-live="polite" className="py-3 text-sm text-muted-foreground">
          {copy.linkedDevicesLoading}
        </div>
      ) : null}

      {loadFailed ? (
        <ListRow
          action={
            <Button onClick={() => void load()} size="sm" variant="secondary">
              {t.common.retry}
            </Button>
          }
          title={copy.linkedDevicesError}
        />
      ) : null}

      {devices !== null && uniqueDevices.length === 0 ? (
        <ListRow description={copy.linkedDevicesEmptyDesc} title={copy.linkedDevicesEmpty} />
      ) : null}

      {uniqueDevices.map(device => {
        const dates = copy.linkedDevicesDates(
          new Date(device.created_at * 1000).toLocaleDateString(),
          new Date(device.last_seen_at * 1000).toLocaleDateString()
        )

        let action = (
          <Button onClick={() => setConfirmingId(device.id)} size="sm" variant="destructive">
            {copy.linkedDevicesRevoke}
          </Button>
        )

        if (confirmingId === device.id) {
          action = (
            <div className="flex gap-2">
              <Button
                disabled={revokingId === device.id}
                onClick={() => void revoke(device)}
                size="sm"
                variant="destructive"
              >
                {revokingId === device.id ? copy.linkedDevicesRevoking : t.common.confirm}
              </Button>
              <Button
                disabled={revokingId === device.id}
                onClick={() => setConfirmingId(null)}
                size="sm"
                variant="secondary"
              >
                {t.common.cancel}
              </Button>
            </div>
          )
        }

        return <ListRow action={action} description={dates} key={device.id} title={device.label} />
      })}
    </SettingsSection>
  )
}
