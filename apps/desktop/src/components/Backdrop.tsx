import { useStore } from '@nanostores/react'

import { $backdrop } from '@/store/backdrop'
import { useTheme } from '@/themes/context'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

const isHttpUrl = (value: string) =>
  /^https?:\/\//i.test(value) || value.startsWith('data:') || value.startsWith('file:')

const isAbsPath = (value: string) => /^([A-Za-z]:[\\/]|\\\\|\/)/.test(value)

export function Backdrop() {
  const on = useStore($backdrop)
  const { theme } = useTheme()
  // Use only the skin-provided wallpaper. A hardcoded local fallback causes
  // palette/wallpaper mismatch (e.g. light palette over portrait background).
  const wallpaper = (theme.backgroundImage ?? '').trim()

  if (!on) {
    return null
  }

  const skinFit = theme.backgroundImageFit || 'cover'
  const skinPosition = theme.backgroundImagePosition || 'center'
  // When a wallpaper is active, the chat surface must be opaque so text stays
  // readable over the image. We signal this via a CSS class on <html>.
  const hasWallpaper = !!skinWallpaperUrl

  useEffect(() => {
    document.documentElement.classList.toggle('has-skin-wallpaper', hasWallpaper)

    return () => document.documentElement.classList.remove('has-skin-wallpaper')
  }, [hasWallpaper])

  const skinOverlay = theme.backgroundOverlay || ''

  const skinLayer = useMemo(() => {
    if (!skinWallpaperUrl) {
      return null
    }

    return (
      <div aria-hidden className="pointer-events-none absolute inset-0 z-0 overflow-hidden">
        <img
          alt=""
          className="h-full w-full"
          fetchPriority="low"
          src={skinWallpaperUrl}
          style={{
            objectFit: skinFit as CSSProperties['objectFit'],
            objectPosition: skinPosition
          }}
        />
        {skinOverlay ? <div className="absolute inset-0" style={{ background: skinOverlay }} /> : null}
      </div>
    )
  }, [skinFit, skinOverlay, skinPosition, skinWallpaperUrl])

  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 z-2 opacity-[0.025] mix-blend-difference">
      <img
        alt=""
        className="h-[160dvh] w-auto min-w-dvw object-cover object-left-top [filter:invert(var(--backdrop-invert-mul,1))]"
        fetchPriority="low"
        src={assetPath('ds-assets/filler-bg0.jpg')}
      />
    </div>
  )
}
