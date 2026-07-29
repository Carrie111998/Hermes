import { useStore } from '@nanostores/react'
import { Leva, useControls } from 'leva'
import { type CSSProperties, useEffect, useMemo, useState } from 'react'

import { $backdrop } from '@/store/backdrop'
import { useTheme } from '@/themes/context'

const BLEND_MODES = [
  'normal',
  'multiply',
  'screen',
  'overlay',
  'darken',
  'lighten',
  'color-dodge',
  'color-burn',
  'hard-light',
  'soft-light',
  'difference',
  'exclusion',
  'hue',
  'saturation',
  'color',
  'luminosity'
] as const

type BlendMode = (typeof BLEND_MODES)[number]
const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

const isHttpUrl = (value: string) => /^https?:\/\//i.test(value) || value.startsWith('data:') || value.startsWith('file:')
const isAbsPath = (value: string) => /^([A-Za-z]:[\\/]|\\\\|\/)/.test(value)

export function Backdrop() {
  const [controlsOpen, setControlsOpen] = useState(false)
  const [skinWallpaperUrl, setSkinWallpaperUrl] = useState<string | null>(null)
  const on = useStore($backdrop)
  const { theme } = useTheme()

  useEffect(() => {
    if (!import.meta.env.DEV) {
      return
    }

    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null

      const editing =
        target?.isContentEditable ||
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement

      if (editing || event.repeat || event.altKey || event.ctrlKey || event.metaKey) {
        return
      }

      if (event.shiftKey && event.code === 'KeyY') {
        setControlsOpen(open => !open)
      }
    }

    window.addEventListener('keydown', onKeyDown)

    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  // Resolve skin wallpaper → data URL via Electron when needed.
  useEffect(() => {
    let cancelled = false
    const raw = (theme.backgroundImage ?? '').trim()

    if (!raw) {
      setSkinWallpaperUrl(null)
      return
    }

    if (isHttpUrl(raw)) {
      setSkinWallpaperUrl(raw)
      return
    }

    const path = isAbsPath(raw) ? raw : undefined
    if (!path || !window.hermesDesktop?.readFileDataUrl) {
      // Relative skin filenames need HERMES_HOME; fall back to default statue.
      setSkinWallpaperUrl(null)
      return
    }

    void window.hermesDesktop
      .readFileDataUrl(path)
      .then(url => {
        if (!cancelled) {
          setSkinWallpaperUrl(url || null)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSkinWallpaperUrl(null)
        }
      })

    return () => {
      cancelled = true
    }
  }, [theme.backgroundImage])

  // Relative skin filenames: try a few common HERMES_HOME locations.
  useEffect(() => {
    let cancelled = false
    const raw = (theme.backgroundImage ?? '').trim()
    if (!raw || isHttpUrl(raw) || isAbsPath(raw) || !window.hermesDesktop?.readFileDataUrl) {
      return
    }

    const homes = [`C:/Users/downl/.hermes/skins/${raw}`]

    ;(async () => {
      for (const candidate of homes) {
        try {
          const url = await window.hermesDesktop!.readFileDataUrl(candidate)
          if (!cancelled && url) {
            setSkinWallpaperUrl(url)
            return
          }
        } catch {
          // try next
        }
      }
    })()

    return () => {
      cancelled = true
    }
  }, [theme.backgroundImage])

  const shape = useControls(
    'UI / Shape',
    { radiusScalar: { value: 0.2, min: 0, max: 2, step: 0.1, label: 'radius scalar' } },
    { collapsed: true }
  )

  useEffect(() => {
    document.documentElement.style.setProperty('--radius-scalar', String(shape.radiusScalar))
  }, [shape.radiusScalar])

  const statue = useControls(
    'Backdrop / Statue',
    {
      enabled: { value: true, label: 'on' },
      opacity: { value: 0.025, min: 0, max: 1, step: 0.005 },
      blendMode: { value: 'difference' as BlendMode, options: BLEND_MODES, label: 'blend' },
      invert: { value: true, label: 'invert color' },
      saturate: { value: 1, min: 0, max: 3, step: 0.05, label: 'saturate' },
      brightness: { value: 1, min: 0, max: 2, step: 0.05, label: 'brightness' },
      objectPosition: {
        value: 'top left',
        options: ['top left', 'top right', 'bottom left', 'bottom right', 'center', 'top', 'bottom', 'left', 'right'],
        label: 'position'
      },
      scale: { value: 160, min: 100, max: 300, step: 5, label: 'height (dvh)' }
    },
    { collapsed: true }
  )

  const skinFit = theme.backgroundImageFit || 'cover'
  const skinPosition = theme.backgroundImagePosition || 'center'
  const skinOverlay = theme.backgroundOverlay || ''

  const skinLayer = useMemo(() => {
    if (!skinWallpaperUrl) {
      return null
    }

    return (
      <div aria-hidden className="pointer-events-none absolute inset-0 z-1 overflow-hidden">
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
    <>
      <Leva collapsed hidden={!import.meta.env.DEV || !controlsOpen} titleBar={{ title: 'backdrop', drag: true }} />

      {skinLayer}

      {on && statue.enabled && !skinWallpaperUrl && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 z-2"
          style={{
            mixBlendMode: statue.blendMode as CSSProperties['mixBlendMode'],
            opacity: statue.opacity
          }}
        >
          <img
            alt=""
            className="w-auto min-w-dvw object-cover"
            fetchPriority="low"
            src={assetPath('ds-assets/filler-bg0.jpg')}
            style={{
              height: `${statue.scale}dvh`,
              objectPosition: statue.objectPosition,
              filter: `invert(calc(${statue.invert ? 1 : 0} * var(--backdrop-invert-mul, 1))) saturate(${statue.saturate}) brightness(${statue.brightness})`
            }}
          />
        </div>
      )}
    </>
  )
}
