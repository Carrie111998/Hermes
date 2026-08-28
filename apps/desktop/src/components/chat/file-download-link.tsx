import { useStore } from '@nanostores/react'
import { type ComponentProps, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { translateNow } from '@/i18n'
import { downloadGatewayMediaFile, mediaExternalUrl, mediaName } from '@/lib/media'
import { isUnsafeRevealPath } from '@/lib/reveal-path-guard'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'

interface FileDownloadLinkProps extends Omit<ComponentProps<'a'>, 'download' | 'href' | 'onClick'> {
  path: string
}

/**
 * A real filename link whose primary action saves a backend-hosted file.
 *
 * Electron owns the authenticated streaming + native Save dialog for both
 * local and remote gateways. The ordinary anchor/download attributes remain a
 * browser fallback, while Desktop intercepts the click so local files are
 * copied instead of opened in their associated application.
 */
export function FileDownloadLink({ children, className, path, ...props }: FileDownloadLinkProps) {
  const storedSessionId = useStore(useSessionView().$storedId)
  const [saving, setSaving] = useState(false)
  const name = mediaName(path)
  const unsafe = isUnsafeRevealPath(path)

  const save = async () => {
    if (saving || unsafe) {
      return
    }

    setSaving(true)

    try {
      const result = await downloadGatewayMediaFile(path, storedSessionId)

      if (result.saved && !result.canceled) {
        notify({ durationMs: 1500, kind: 'info', message: translateNow('fileMenu.downloadSaved') })
      }
    } catch (error) {
      notifyError(error, translateNow('fileMenu.downloadFailed'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <a
      aria-busy={saving || undefined}
      className={cn('ref wrap-anywhere', className)}
      download={name}
      href={unsafe ? '#' : mediaExternalUrl(path)}
      onClick={event => {
        if (unsafe) {
          event.preventDefault()
          notifyError(new Error('Unsafe path'), translateNow('fileMenu.downloadFailed'))

          return
        }

        if (window.hermesDesktop) {
          event.preventDefault()
          void save()
        }
      }}
      {...props}
    >
      {children ?? name}
    </a>
  )
}
