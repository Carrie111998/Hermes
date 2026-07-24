'use client'

import { motion } from 'motion/react'
import { useMemo, type ComponentProps, InputHTMLAttributes, ReactNode } from 'react'

import { type OpticalGlassOptions, useOpticalGlass } from '@/hooks/use-optical-glass'
import { useTheme } from '@/themes/context'
import { cn } from '@/lib/utils'

/**
 * AURA glass system (ported from Albert-Einstein-UI to Hermes apps/desktop).
 *
 * Each component renders the eight AURA layers as aria-hidden siblings:
 *   umbra → volume → caustic → body → bevel → inner → glare → fresnel
 *
 * The contents sit in `.aura-content` above all layers (z-index 7).
 *
 * NOTE: we intentionally do NOT import `@base-ui/react` (Hermes' UI layer uses
 * `radix-ui`). `GlassButton` is exposed as a styling surface — when a real
 * `<button>` is needed, callers wrap their existing radix-based control in
 * `GlassCard` directly. This keeps the system library-only and avoids pulling
 * in another UI runtime.
 */

export type GlassCardProps = Omit<ComponentProps<typeof motion.div>, keyof OpticalGlassOptions> &
  OpticalGlassOptions & {
    children?: ReactNode
    hoverable?: boolean
    surface?: 'clear' | 'smoke' | 'dense'
  }

export function GlassCard({
  children,
  className,
  light,
  depth,
  bezel,
  thickness,
  radius,
  tracking,
  hoverable = false,
  surface = 'smoke',
  ...props
}: GlassCardProps) {
  const optical = useOpticalGlass({ light, depth, bezel, thickness, radius, tracking })

  return (
    <motion.div
      className={cn('aura-glass', `aura-${surface}`, className)}
      ref={optical.surfaceRef}
      style={optical.style}
      transition={{ type: 'spring', stiffness: 380, damping: 32 }}
      whileHover={hoverable ? { y: -1, scale: 1.002 } : undefined}
      whileTap={hoverable ? { scale: 0.997 } : undefined}
      {...optical.handlers}
      {...props}
    >
      <span aria-hidden="true" className="aura-umbra" />
      <span aria-hidden="true" className="aura-volume" />
      <span aria-hidden="true" className="aura-caustic" />
      <span aria-hidden="true" className="aura-body" />
      <span aria-hidden="true" className="aura-bevel" />
      <span aria-hidden="true" className="aura-inner" />
      <span aria-hidden="true" className="aura-glare" />
      <span aria-hidden="true" className="aura-fresnel" />
      <span className="aura-content">{children}</span>
    </motion.div>
  )
}

type GlassPanelProps = GlassCardProps & {
  eyebrow?: string
  title?: string
  action?: ReactNode
}

/**
 * Glass-panel: a glass card with an optional title strip. The header is a
 * small label row inside `.glass-panel-header`; the body uses
 * `.glass-panel-body`. Both classes are defined in styles.css.
 */
export function GlassPanel({
  eyebrow,
  title,
  action,
  children,
  className,
  ...props
}: GlassPanelProps) {
  return (
    <GlassCard
      bezel="thin"
      className={cn('glass-panel', className)}
      depth="thin"
      thickness="thin"
      {...props}
    >
      {(eyebrow || title || action) && (
        <header className="glass-panel-header">
          <span>
            {eyebrow && <small>{eyebrow}</small>}
            {title && <strong>{title}</strong>}
          </span>
          {action}
        </header>
      )}
      <div className="glass-panel-body">{children}</div>
    </GlassCard>
  )
}

/**
 * Glass-input: a glass card wrapping an input, with optional leading/trailing
 * adornments. The CSS expects `.glass-input`, `.glass-input-leading`,
 * `.glass-input-trailing` to be styled under `.aura-content`.
 */
type GlassInputProps = InputHTMLAttributes<HTMLInputElement> & {
  leading?: ReactNode
  trailing?: ReactNode
}

/**
 * `useAuraActive` — true when the Aura skin is the active theme.
 *
 * Reads from `useTheme().themeName` (synced via `applyTheme()` which sets
 * `data-aura="true"` on `:root`). Components that inject `<AuraLayers>` use
 * this to avoid mounting the 8-layer span tree on non-Aura skins (cheaper +
 * keeps markup clean for the default theme).
 */
export function useAuraActive(): boolean {
  const { themeName } = useTheme()
  return themeName === 'aura'
}

/**
 * `<AuraLayers>` — the eight AURA optical glass layers as real DOM spans,
 * matching the structure rendered by `<GlassCard>` in Albert-Einstein-UI.
 *
 * Usage: insert as the FIRST child of an existing UI surface
 * (`<div data-slot="...">...<AuraLayers/>...</div>`). The CSS in styles.css
 * targets `[data-aura='true'] [data-slot='...']` to scope the layer
 * variables (`--glass-radius`, `--inner-radius`, `--glass-bezel`, …) and the
 * `position: relative; isolation: isolate` hosting rules. When AURA is
 * disabled the `[data-aura]` gate hides every `.aura-*` span via
 * `display: none`, so the markup renders as harmless empty siblings on
 * non-Aura skins.
 *
 * The surface variant (`clear | smoke | dense`) controls how translucent the
 * `.aura-inner` body fill is — same contract as `<GlassCard>`'s `surface`
 * prop. Light coordinates default to the AURA "top-right" preset (76% 12%)
 * via the `.aura-host` variable block in styles.css.
 */
export type AuraLayersProps = {
  surface?: 'clear' | 'smoke' | 'dense'
}

export function AuraLayers({ surface = 'smoke' }: AuraLayersProps) {
  // Mirror the exact sibling order emitted by GlassCard:
  //   umbra → volume → caustic → body → bevel → inner → glare → fresnel
  // Putting them in a single parent `.aura-host` keeps all eight layers
  // contained to the host's stacking context and lets CSS scope the glass
  // variables on the host element rather than on document root.
  return (
    <span aria-hidden="true" className={cn('aura-host', `aura-${surface}`)}>
      <span aria-hidden="true" className="aura-umbra" />
      <span aria-hidden="true" className="aura-volume" />
      <span aria-hidden="true" className="aura-caustic" />
      <span aria-hidden="true" className="aura-body" />
      <span aria-hidden="true" className="aura-bevel" />
      <span aria-hidden="true" className="aura-inner" />
      <span aria-hidden="true" className="aura-glare" />
      <span aria-hidden="true" className="aura-fresnel" />
    </span>
  )
}

export function GlassInput({ className, leading, trailing, ...props }: GlassInputProps) {
  return (
    <GlassCard
      bezel="thin"
      className={cn('glass-input', className)}
      depth="thin"
      radius={9}
      surface="clear"
      thickness="thin"
    >
      <span className="glass-input-leading">{leading}</span>
      <input {...props} />
      <span className="glass-input-trailing">{trailing}</span>
    </GlassCard>
  )
}
