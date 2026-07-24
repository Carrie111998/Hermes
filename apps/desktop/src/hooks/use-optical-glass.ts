import { useReducedMotion } from 'motion/react'
import type { CSSProperties, PointerEventHandler } from 'react'
import { useCallback, useMemo, useRef } from 'react'

/**
 * AURA optical-glass hook (ported from Albert-Einstein-UI).
 *
 * Calculates per-pointer CSS custom properties for the 8-layer glass system
 * (`--light-x/y`, `--light-nx/ny`, `--fresnel`, `--incidence`, `--transmission`,
 * `--caustic-x/y`, `--shadow-x/y`, `--light-angle`, `--light-elevation`).
 *
 * The hook is render-only — it writes to the node via inline `style` and
 * `onPointerMove`/`onPointerLeave` handlers that the caller spreads onto a
 * `motion.div` (or any element). Everything is theme-driven via CSS; this
 * hook never hardcodes colors.
 *
 * Respects `prefers-reduced-motion`: when enabled, tracking is disabled and
 * the fallback light position is used as a static style.
 */

export type GlassScale = 'thin' | 'regular' | 'thick' | 'ultra'

export type OpticalLight = {
  x?: number
  y?: number
  intensity?: number
  elevation?: number
  temperature?: 'cool' | 'neutral' | 'warm'
}

export type OpticalGlassOptions = {
  light?: OpticalLight
  depth?: number | GlassScale
  bezel?: number | GlassScale
  thickness?: number | GlassScale
  radius?: number
  tracking?: boolean
}

type OpticalStyle = CSSProperties & Record<`--${string}`, string | number>

const depthScale: Record<GlassScale, number> = { thin: 4, regular: 10, thick: 18, ultra: 28 }
const bezelScale: Record<GlassScale, number> = { thin: 1, regular: 2, thick: 3, ultra: 5 }
const thicknessScale: Record<GlassScale, number> = { thin: 3, regular: 7, thick: 12, ultra: 18 }

function resolve(value: number | GlassScale | undefined, fallback: number, scale: Record<GlassScale, number>) {
  return typeof value === 'number' ? value : value ? scale[value] : fallback
}

export function useOpticalGlass({
  light = {},
  depth = 'regular',
  bezel = 'regular',
  thickness = 'regular',
  radius = 18,
  tracking = true,
}: OpticalGlassOptions = {}) {
  const surfaceRef = useRef<HTMLDivElement>(null)
  const reducedMotion = useReducedMotion()
  const fallbackX = light.x ?? 76
  const fallbackY = light.y ?? 12
  const resolvedDepth = resolve(depth, 10, depthScale)
  const resolvedBezel = resolve(bezel, 2, bezelScale)
  const resolvedThickness = resolve(thickness, 7, thicknessScale)

  const writeLight = useCallback((clientX: number, clientY: number) => {
    const node = surfaceRef.current

    if (!node) {return}
    const rect = node.getBoundingClientRect()
    const px = Math.min(100, Math.max(0, ((clientX - rect.left) / rect.width) * 100))
    const py = Math.min(100, Math.max(0, ((clientY - rect.top) / rect.height) * 100))
    const nx = px / 50 - 1
    const ny = py / 50 - 1
    const radial = Math.min(1, Math.hypot(nx, ny) / Math.SQRT2)
    const cosTheta = Math.max(0, 1 - radial)
    const f0 = 0.04
    const fresnel = f0 + (1 - f0) * Math.pow(1 - cosTheta, 5)
    const elevation = light.elevation ?? 0.72
    const direction = Math.atan2(ny, nx) * (180 / Math.PI)

    node.style.setProperty('--light-x', `${px}%`)
    node.style.setProperty('--light-y', `${py}%`)
    node.style.setProperty('--light-nx', nx.toFixed(4))
    node.style.setProperty('--light-ny', ny.toFixed(4))
    node.style.setProperty('--light-angle', `${direction}deg`)
    node.style.setProperty('--incidence', radial.toFixed(4))
    node.style.setProperty('--fresnel', fresnel.toFixed(4))
    node.style.setProperty('--transmission', (1 - fresnel).toFixed(4))
    node.style.setProperty('--light-elevation', elevation.toFixed(3))
    node.style.setProperty('--caustic-x', `${50 + nx * 24}%`)
    node.style.setProperty('--caustic-y', `${50 + ny * 24}%`)
    node.style.setProperty('--shadow-x', `${nx * -resolvedDepth * 0.45}px`)
    node.style.setProperty('--shadow-y', `${resolvedDepth * (0.55 - ny * 0.18)}px`)
  }, [light.elevation, resolvedDepth])

  const onPointerMove = useCallback<PointerEventHandler<HTMLDivElement>>((event) => {
    if (tracking && !reducedMotion && event.pointerType !== 'touch') {writeLight(event.clientX, event.clientY)}
  }, [reducedMotion, tracking, writeLight])

  const onPointerLeave = useCallback(() => {
    const node = surfaceRef.current

    if (!node) {return}
    node.style.setProperty('--light-x', `${fallbackX}%`)
    node.style.setProperty('--light-y', `${fallbackY}%`)
    node.style.setProperty('--light-nx', '.52')
    node.style.setProperty('--light-ny', '-.76')
    node.style.setProperty('--fresnel', '.07')
    node.style.setProperty('--transmission', '.93')
  }, [fallbackX, fallbackY])

  // The base style intentionally avoids baking any color in: the CSS in
  // styles.css is expected to resolve `--light-color` / `--bounce-color`
  // from the active DesktopTheme tokens, so the same hook works against
  // every theme (including the default "nous" theme).
  const style = useMemo<OpticalStyle>(() => ({
    '--light-x': `${fallbackX}%`,
    '--light-y': `${fallbackY}%`,
    '--light-nx': .52,
    '--light-ny': -.76,
    '--light-angle': '-56deg',
    '--light-intensity': light.intensity ?? .82,
    '--light-elevation': light.elevation ?? .72,
    '--glass-depth': `${resolvedDepth}px`,
    '--glass-bezel': `${resolvedBezel}px`,
    '--glass-thickness': `${resolvedThickness}px`,
    '--glass-radius': `${radius}px`,
    '--inner-radius': `${Math.max(0, radius - resolvedBezel - 2)}px`,
    '--incidence': .46,
    '--fresnel': .07,
    '--transmission': .93,
    '--caustic-x': '62%',
    '--caustic-y': '32%',
    '--shadow-x': `${resolvedDepth * -.2}px`,
    '--shadow-y': `${resolvedDepth * .7}px`,
  }), [fallbackX, fallbackY, light.elevation, light.intensity, radius, resolvedBezel, resolvedDepth, resolvedThickness])

  return { surfaceRef, style, handlers: { onPointerMove, onPointerLeave } }
}
