import { useEffect } from 'react'

import { getHermesConfigRecord, saveHermesConfig } from '@/hermes'
import { useI18n } from '@/i18n'
import {
  hasSeenFileCheckpointFirstUse,
  isCheckpointsEnabledInConfig,
  markFileCheckpointFirstUseSeen,
  withCheckpointsEnabled
} from '@/lib/file-checkpoints'
import { confirm } from '@/store/confirm'
import { activeGateway } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { $activeSessionId } from '@/store/session'

export function useFileCheckpointFirstUse(gatewayOpen: boolean): void {
  const { t } = useI18n()
  const copy = t.assistant.fileCheckpoints

  useEffect(() => {
    if (!gatewayOpen) {
      return
    }

    const profile = $activeGatewayProfile.get() || 'default'

    if (hasSeenFileCheckpointFirstUse(profile, window.localStorage)) {
      return
    }

    markFileCheckpointFirstUseSeen(profile, window.localStorage)

    void (async () => {
      try {
        const current = await getHermesConfigRecord(profile)

        if (isCheckpointsEnabledInConfig(current)) {
          return
        }

        const ok = await confirm({
          cancelLabel: copy.firstUseSkip,
          confirmLabel: copy.firstUseConfirm,
          description: copy.firstUseBody,
          title: copy.firstUseTitle
        })

        if (!ok) {
          return
        }

        await saveHermesConfig(withCheckpointsEnabled(current), profile)
        const sessionId = $activeSessionId.get()
        const gateway = activeGateway()

        if (sessionId && gateway) {
          await gateway.request('rollback.list', { session_id: sessionId })
        }

        notify({ message: copy.enabledToast })
      } catch (err) {
        notifyError(err, copy.loadFailed)
      }
    })()
  }, [
    copy.enabledToast,
    copy.firstUseBody,
    copy.firstUseConfirm,
    copy.firstUseSkip,
    copy.firstUseTitle,
    copy.loadFailed,
    gatewayOpen
  ])
}
