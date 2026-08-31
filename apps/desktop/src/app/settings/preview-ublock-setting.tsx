import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { useI18n } from '@/i18n'
import { notifyError } from '@/store/notifications'
import { $previewUblock, loadPreviewUblock, setPreviewUblockEnabled } from '@/store/preview-ublock'

import { ToggleRow } from './primitives'

export function PreviewUblockSetting() {
  const { t } = useI18n()
  const copy = t.settings.config
  const previewUblock = useStore($previewUblock)
  const [pending, setPending] = useState(false)

  useEffect(() => {
    void loadPreviewUblock()
  }, [])

  const handleChange = async (enabled: boolean) => {
    setPending(true)

    try {
      await setPreviewUblockEnabled(enabled)
    } catch (error) {
      notifyError(error, copy.previewUblockFailure)
      await loadPreviewUblock()
    } finally {
      setPending(false)
    }
  }

  return (
    <ToggleRow
      below={
        pending ? (
          <div className="mt-1 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
            {copy.previewUblockDownloading}
          </div>
        ) : undefined
      }
      checked={previewUblock.enabled}
      description={copy.previewUblockDescription}
      disabled={pending}
      label={copy.previewUblockTitle}
      onChange={on => void handleChange(on)}
    />
  )
}
