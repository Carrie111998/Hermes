'use client'

import { type ReactNode, useEffect, useRef, useState } from 'react'

import { Dialog, DialogContent } from '@/components/ui/dialog'
import { Tip } from '@/components/ui/tooltip'
import { Check, Copy, Maximize, RefreshCw, X, ZoomIn, ZoomOut } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { useZoomPan } from './use-zoom-pan'

interface ZoomableProps {
  /** Inline content; also the default full-view content. */
  children: ReactNode
  /** Full-view content, if it should differ from the inline version. */
  overlay?: ReactNode
  /** Copy/export action shown in the viewer toolbar. */
  onCopy?: () => Promise<void> | void
  /** Accessible label for the expand affordance. */
  label?: string
  className?: string
}

/**
 * Generic click-to-expand viewer: renders inline content with a hover "expand"
 * affordance, then opens a full overlay where the content can be panned/zoomed
 * (see useZoomPan) and optionally copied. Content-agnostic — wrap a diagram,
 * image, or any node.
 */
export function Zoomable({ children, overlay, onCopy, label = 'Open full view', className }: ZoomableProps) {
  const [open, setOpen] = useState(false)

  return (
    <>
      <div className={cn('group/zoomable relative', className)}>
        {/* The whole content is the trigger — click anywhere to open, like an image. */}
        <button
          className="block w-full cursor-zoom-in text-left"
          onClick={() => setOpen(true)}
          title={label}
          type="button"
        >
          {children}
        </button>
        <span
          aria-hidden
          className="pointer-events-none absolute right-2 top-2 grid size-8 place-items-center rounded-full border border-border/70 bg-background/80 text-muted-foreground opacity-0 shadow-sm backdrop-blur transition-opacity group-hover/zoomable:opacity-100"
        >
          <Maximize className="size-4" />
        </span>
      </div>
      {open && (
        <ZoomPanViewer onCopy={onCopy} onOpenChange={setOpen} open={open}>
          {overlay ?? children}
        </ZoomPanViewer>
      )}
    </>
  )
}

function ZoomPanViewer({
  children,
  onCopy,
  onOpenChange,
  open
}: {
  children: ReactNode
  onCopy?: () => Promise<void> | void
  onOpenChange: (open: boolean) => void
  open: boolean
}) {
  const { fit, panning, reset, stageProps, style, zoomIn, zoomOut } = useZoomPan()
  const stageRef = useRef<HTMLDivElement | null>(null)
  const contentRef = useRef<HTMLDivElement | null>(null)

  // Fit content to the stage on open. Content smaller than the stage (the
  // common case for mermaid diagrams) would otherwise open at natural size in
  // the middle of empty space — reads as "zoomed out". Measure the laid-out
  // content at scale 1 (already CSS-capped to the viewport) and scale up to
  // fill; oversized content is never scaled down below its fitted size.
  useEffect(() => {
    if (!open) {
      return
    }

    const raf = requestAnimationFrame(() => {
      const stage = stageRef.current
      const content = contentRef.current

      if (!stage || !content) {
        return
      }

      const stageRect = stage.getBoundingClientRect()
      const contentRect = content.getBoundingClientRect()

      if (stageRect.width <= 0 || stageRect.height <= 0 || contentRect.width <= 0 || contentRect.height <= 0) {
        return
      }

      fit(Math.min(stageRect.width / contentRect.width, stageRect.height / contentRect.height))
    })

    return () => cancelAnimationFrame(raf)
  }, [open, fit])

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent
        className="flex h-[85vh] w-[90vw] max-w-[90vw] flex-col gap-0 overflow-hidden p-0"
        showCloseButton={false}
      >
        <div
          className={cn(
            'relative flex-1 touch-none select-none overflow-hidden',
            panning ? 'cursor-grabbing' : 'cursor-grab'
          )}
          ref={stageRef}
          {...stageProps}
        >
          <div className="absolute inset-0 grid place-items-center">
            <div className="origin-center" ref={contentRef} style={style}>
              {children}
            </div>
          </div>
        </div>
        <Toolbar onClose={() => onOpenChange(false)} onCopy={onCopy} reset={reset} zoomIn={zoomIn} zoomOut={zoomOut} />
      </DialogContent>
    </Dialog>
  )
}

function Toolbar({
  onClose,
  onCopy,
  reset,
  zoomIn,
  zoomOut
}: {
  onClose: () => void
  onCopy?: () => Promise<void> | void
  reset: () => void
  zoomIn: () => void
  zoomOut: () => void
}) {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    if (!onCopy) {
      return
    }

    await onCopy()
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1 rounded-full border border-border/70 bg-background/85 p-1 shadow-sm backdrop-blur">
      <ToolbarButton label="Zoom out" onClick={zoomOut}>
        <ZoomOut className="size-4" />
      </ToolbarButton>
      <ToolbarButton label="Reset" onClick={reset}>
        <RefreshCw className="size-4" />
      </ToolbarButton>
      <ToolbarButton label="Zoom in" onClick={zoomIn}>
        <ZoomIn className="size-4" />
      </ToolbarButton>
      {onCopy && (
        <>
          <Divider />
          <ToolbarButton label={copied ? 'Copied' : 'Copy'} onClick={() => void copy()}>
            {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
          </ToolbarButton>
        </>
      )}
      <Divider />
      <ToolbarButton label="Close" onClick={onClose}>
        <X className="size-4" />
      </ToolbarButton>
    </div>
  )
}

function Divider() {
  return <span className="mx-0.5 h-5 w-px bg-border" />
}

function ToolbarButton({ children, label, onClick }: { children: ReactNode; label: string; onClick: () => void }) {
  return (
    <Tip label={label}>
      <button
        aria-label={label}
        className="grid size-8 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        onClick={onClick}
        type="button"
      >
        {children}
      </button>
    </Tip>
  )
}
