/**
 * VIEWPORT BAR — the presets, the free size, and the rotate.
 *
 * Sits under the address bar like the pin panel does, and like it, is only on
 * screen while the user is using it. `null` means "fill the pane", which is the
 * behaviour that existed before this and stays the default.
 */

import { useEffect, useState } from 'react'

import { Codicon } from '@/components/ui/codicon'
import {
  customViewport,
  parseEdge,
  rotateViewport,
  type Viewport,
  VIEWPORT_PRESETS,
  viewportLabel
} from '@/lib/preview-viewport'
import { cn } from '@/lib/utils'

interface PreviewViewportBarProps {
  onChange: (viewport: null | Viewport) => void
  open: boolean
  /** What the pane settled on, so the readout shows the real zoom. */
  scale: number
  viewport: null | Viewport
}

export function PreviewViewportBar({ onChange, open, scale, viewport }: PreviewViewportBarProps) {
  const [width, setWidth] = useState('')
  const [height, setHeight] = useState('')

  // Follow the active size, so switching to a preset fills the boxes and the
  // user can nudge one edge instead of retyping both.
  useEffect(() => {
    setWidth(viewport ? String(viewport.width) : '')
    setHeight(viewport ? String(viewport.height) : '')
  }, [viewport])

  if (!open) {return null}

  const applyCustom = () => {
    const w = parseEdge(width)
    const h = parseEdge(height)

    // One edge on its own is still meaningful — keep the other.
    if (!w && !h) {return}
    onChange(customViewport(w ?? viewport?.width ?? 1280, h ?? viewport?.height ?? 800))
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5 border-t border-border/60 px-3 py-2 text-xs">
      <button
        className={cn(
          'rounded px-2 py-1 font-medium transition-colors',
          viewport ? 'bg-muted hover:bg-muted/70' : 'bg-primary text-primary-foreground'
        )}
        onClick={() => onChange(null)}
        title="Fill the pane, as before"
        type="button"
      >
        Fit
      </button>

      {VIEWPORT_PRESETS.map(preset => (
        <button
          className={cn(
            'rounded px-2 py-1 transition-colors',
            viewport?.id === preset.id && viewport.width === preset.width
              ? 'bg-primary text-primary-foreground'
              : 'bg-muted hover:bg-muted/70'
          )}
          key={preset.id}
          onClick={() => onChange(preset)}
          title={`${preset.width}×${preset.height}${preset.mobile ? ' · mobile' : ''}`}
          type="button"
        >
          {preset.label}
        </button>
      ))}

      <div className="ms-auto flex items-center gap-1">
        {/* Free values, because no preset list is ever the size someone needs. */}
        <input
          aria-label="Viewport width"
          className="w-14 rounded border border-border/60 bg-background px-1.5 py-1 text-center tabular-nums"
          inputMode="numeric"
          onBlur={applyCustom}
          onChange={event => setWidth(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') {applyCustom()}
          }}
          placeholder="w"
          value={width}
        />
        <span className="text-muted-foreground">×</span>
        <input
          aria-label="Viewport height"
          className="w-14 rounded border border-border/60 bg-background px-1.5 py-1 text-center tabular-nums"
          inputMode="numeric"
          onBlur={applyCustom}
          onChange={event => setHeight(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') {applyCustom()}
          }}
          placeholder="h"
          value={height}
        />
        <button
          aria-label="Rotate"
          className="rounded px-1.5 py-1 hover:bg-muted disabled:opacity-40"
          disabled={!viewport}
          onClick={() => viewport && onChange(rotateViewport(viewport))}
          title="Swap width and height"
          type="button"
        >
          <Codicon name="screen-normal" size="0.8125rem" />
        </button>
      </div>

      {viewport && (
        <span className="w-full text-muted-foreground">
          {viewportLabel(viewport, scale)}
          {viewport.mobile && ' · mobile'}
        </span>
      )}
    </div>
  )
}
