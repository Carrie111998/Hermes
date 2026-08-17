import { useCallback, useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { ImageIcon, Loader2, Trash2 } from '@/lib/icons'

import { ListRow, SectionHeading } from './primitives'

type IntroImageBridge = NonNullable<Window['hermesDesktop']>['introImage']

function getBridge(): IntroImageBridge | null {
  return window.hermesDesktop?.introImage ?? null
}

/**
 * Appearance opt-in for the new-session welcome screen image. The renderer
 * reads the configured path as a `data:` URL on mount and silently falls back
 * to the HERMES AGENT wordmark when nothing is configured, the file is
 * missing, or the extension is unsupported. Settings here just persist the
 * path — open a fresh chat to see the change.
 */
export function IntroImageSettings() {
  const { t } = useI18n()
  const copy = t.settings.appearance.introImage

  const [imagePath, setImagePath] = useState<string | null>(null)
  const [previewDataUrl, setPreviewDataUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    const bridge = getBridge()

    if (!bridge) {
      return
    }

    const result = await bridge.get()
    setImagePath(result.imagePath)
    setPreviewDataUrl(result.dataUrl)
    setError(result.error)
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const pick = async () => {
    const bridge = getBridge()

    if (!bridge) {
      return
    }

    setBusy(true)

    try {
      const picked = await bridge.pick()

      if (picked.canceled || !picked.imagePath) {
        return
      }

      const saved = await bridge.set(picked.imagePath)
      setImagePath(saved.imagePath)
      await refresh()
      triggerHaptic('crisp')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const clear = async () => {
    const bridge = getBridge()

    if (!bridge) {
      return
    }

    setBusy(true)

    try {
      await bridge.set(null)
      setImagePath(null)
      setPreviewDataUrl(null)
      setError(null)
      triggerHaptic('selection')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <SectionHeading icon={ImageIcon} title={copy.title} />
      <p className="max-w-2xl text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {copy.intro}
      </p>

      <div className="mt-2">
        <ListRow
          action={
            <div className="flex items-center gap-2">
              {imagePath && (
                <Button
                  aria-label={copy.clear}
                  disabled={busy}
                  onClick={clear}
                  size="inline"
                  variant="text"
                >
                  <Trash2 className="size-3.5" />
                  {copy.clear}
                </Button>
              )}
              <Button
                aria-label={imagePath ? copy.replace : copy.choose}
                disabled={busy}
                onClick={pick}
                size="inline"
                variant="default"
              >
                {busy ? (
                  <Loader2 className="size-3.5 animate-spin" />
                ) : (
                  <ImageIcon className="size-3.5" />
                )}
                {imagePath ? copy.replace : copy.choose}
              </Button>
            </div>
          }
          below={
            <>
              {previewDataUrl && (
                <div className="mt-3 flex items-center justify-center rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) p-4">
                  <img
                    alt={copy.previewAlt}
                    className="max-h-32 max-w-full object-contain"
                    src={previewDataUrl}
                  />
                </div>
              )}
              {error && (
                <p className="mt-2 text-[length:var(--conversation-caption-font-size)] text-(--ui-red)">
                  {error}
                </p>
              )}
              {imagePath && !previewDataUrl && !error && (
                <p className="mt-2 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                  {copy.loading}
                </p>
              )}
            </>
          }
          description={imagePath ? copy.pathLabel(imagePath) : copy.empty}
          title={copy.title}
          wide
        />
      </div>
    </div>
  )
}