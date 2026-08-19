/**
 * Viewport picker for the in-app browser bar.
 *
 * Lives beside the address field (not the tab strip). Free-size is the
 * default. Presets and custom W×H lock the guest CSS box; PreviewPane
 * scales that box to fit the host.
 */

import { type FormEvent, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { clampSize, modeSize, VIEWPORT_PRESETS, type ViewportMode, type ViewportPresetId } from './preview-viewport'

export interface PreviewViewportControlProps {
  mode: ViewportMode
  onModeChange: (next: ViewportMode) => void
}

export function PreviewViewportControl({ mode, onModeChange }: PreviewViewportControlProps) {
  const { t } = useI18n()
  const copy = t.preview.web
  const [open, setOpen] = useState(false)
  const locked = modeSize(mode)
  const draft = locked ?? { width: 1280, height: 720 }
  const [width, setWidth] = useState(String(draft.width))
  const [height, setHeight] = useState(String(draft.height))
  const active = mode.kind !== 'free'

  const applyCustom = (event: FormEvent) => {
    event.preventDefault()
    const next = clampSize(Number(width), Number(height))

    if (!Number.isFinite(next.width) || !Number.isFinite(next.height)) {
      return
    }

    setWidth(String(next.width))
    setHeight(String(next.height))
    onModeChange({ kind: 'custom', width: next.width, height: next.height })
    setOpen(false)
  }

  const pick = (next: ViewportMode) => {
    onModeChange(next)
    const size = modeSize(next)

    if (size) {
      setWidth(String(size.width))
      setHeight(String(size.height))
    }

    setOpen(false)
  }

  const label = locked ? `${locked.width}×${locked.height}` : copy.viewportFree

  const presetLabel = (id: ViewportPresetId) => {
    if (id === 'desktop') {
      return copy.viewportDesktop
    }

    if (id === 'laptop') {
      return copy.viewportLaptop
    }

    return copy.viewportMobile
  }

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <Tip label={copy.viewport}>
        <PopoverTrigger asChild>
          <Button
            aria-label={copy.viewport}
            aria-pressed={active || undefined}
            className={cn(
              'self-center bg-transparent select-none',
              active ? 'opacity-100' : 'opacity-60 hover:opacity-100'
            )}
            onPointerDown={event => event.stopPropagation()}
            size="icon-xs"
            type="button"
            variant="ghost"
          >
            <Codicon name={active ? 'device-desktop' : 'screen-full'} size="0.8125rem" />
          </Button>
        </PopoverTrigger>
      </Tip>
      <PopoverContent align="end" className="w-56 p-1.5" side="bottom">
        <div aria-label={copy.viewport} className="flex flex-col gap-0.5" role="listbox">
          <button
            className={cn(
              'flex w-full items-center justify-between rounded-sm px-2 py-1 text-left text-xs',
              mode.kind === 'free' ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/60'
            )}
            onClick={() => pick({ kind: 'free' })}
            role="option"
            type="button"
          >
            <span>{copy.viewportFree}</span>
          </button>
          {VIEWPORT_PRESETS.map(preset => (
            <button
              className={cn(
                'flex w-full items-center justify-between rounded-sm px-2 py-1 text-left text-xs',
                mode.kind === 'preset' && mode.id === preset.id
                  ? 'bg-accent text-accent-foreground'
                  : 'hover:bg-accent/60'
              )}
              key={preset.id}
              onClick={() => pick({ kind: 'preset', id: preset.id })}
              role="option"
              type="button"
            >
              <span>{presetLabel(preset.id)}</span>
              <span className="text-muted-foreground">
                {preset.width}×{preset.height}
              </span>
            </button>
          ))}
          <form className="mt-1 flex items-center gap-1 px-1 py-1" onSubmit={applyCustom}>
            <Input
              aria-label={copy.viewportWidth}
              className="w-14"
              inputMode="numeric"
              onChange={event => setWidth(event.target.value)}
              size="xs"
              value={width}
            />
            <span className="text-muted-foreground text-xs">×</span>
            <Input
              aria-label={copy.viewportHeight}
              className="w-14"
              inputMode="numeric"
              onChange={event => setHeight(event.target.value)}
              size="xs"
              value={height}
            />
            <button className="text-xs underline-offset-2 hover:underline" type="submit">
              {copy.viewportApply}
            </button>
          </form>
          <div className="px-2 pb-0.5 text-[0.625rem] text-muted-foreground">{label}</div>
        </div>
      </PopoverContent>
    </Popover>
  )
}
