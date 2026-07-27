'use client'

import {
  type ComponentProps,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useRef,
  useState
} from 'react'

import { Dialog, DialogContent } from '@/components/ui/dialog'
import { useImageDownload } from '@/hooks/use-image-download'
import { useI18n } from '@/i18n'
import { Download, ZoomIn, ZoomOut } from '@/lib/icons'
import { cn } from '@/lib/utils'

// Zoom bounds for the lightbox. MIN below 1 lets the image shrink slightly
// under the fit size; MAX keeps a long e-commerce detail image readable.
const MIN_SCALE = 0.5
const MAX_SCALE = 8
// Pointer travel (px) below which a gesture counts as a click (closes the
// lightbox) rather than a pan.
const DRAG_THRESHOLD = 4
// Multiplicative step for the wheel and the +/- buttons.
const WHEEL_STEP = 1.1
const BUTTON_STEP = 1.3

export interface ZoomableImageProps extends ComponentProps<'img'> {
  containerClassName?: string
  slot?: string
}

export interface ImageActionCopy {
  downloadImage: string
  savingImage: string
  zoomIn: string
  zoomOut: string
  resetZoom: string
}

interface ViewTransform {
  scale: number
  tx: number
  ty: number
}

const clampScale = (value: number) => Math.min(MAX_SCALE, Math.max(MIN_SCALE, value))

export function ZoomableImage({ className, containerClassName, src, alt, slot, ...props }: ZoomableImageProps) {
  const { t } = useI18n()
  const copy = t.desktop
  const { download, saving } = useImageDownload(src)
  const [lightboxOpen, setLightboxOpen] = useState(false)
  const canOpen = Boolean(src)

  return (
    <>
      <span
        className={cn('group/image relative inline-block max-w-full align-top', containerClassName)}
        data-slot={slot ?? 'aui_zoomable-image'}
      >
        <button
          className="contents"
          disabled={!canOpen}
          onClick={() => canOpen && setLightboxOpen(true)}
          title={canOpen ? copy.openImage : undefined}
          type="button"
        >
          <img alt={alt ?? ''} className={className} src={src} {...props} />
        </button>
        {src && (
          <ImageActionButton className="group-hover/image:opacity-100" copy={copy} onClick={download} saving={saving} />
        )}
      </span>
      {src && (
        <ImageLightbox
          alt={alt}
          copy={copy}
          onClick={download}
          onOpenChange={setLightboxOpen}
          open={lightboxOpen}
          saving={saving}
          src={src}
        />
      )}
    </>
  )
}

export function ImageLightbox({
  alt,
  copy,
  onClick,
  onOpenChange,
  open,
  saving,
  src
}: {
  alt?: string
  copy: ImageActionCopy
  onClick: () => void
  onOpenChange: (open: boolean) => void
  open: boolean
  saving: boolean
  src: string
}) {
  const imgRef = useRef<HTMLImageElement>(null)
  const [view, setView] = useState<ViewTransform>({ scale: 1, tx: 0, ty: 0 })
  const [animate, setAnimate] = useState(false)
  const [grabbing, setGrabbing] = useState(false)

  // Track active pointers for drag (1) and pinch (2), plus whether the current
  // gesture moved far enough to be a pan rather than a click.
  const pointers = useRef(new Map<number, { x: number; y: number }>())
  const pinch = useRef<{ dist: number; midX: number; midY: number } | null>(null)
  const drag = useRef<{ x: number; y: number; startX: number; startY: number } | null>(null)
  const moved = useRef(false)

  // Reset zoom whenever the lightbox opens.
  useEffect(() => {
    if (open) {
      setView({ scale: 1, tx: 0, ty: 0 })
      setAnimate(false)
      moved.current = false
    }
  }, [open])

  const zoomAt = (factor: number, focalRelX: number, focalRelY: number, originX: number, originY: number) => {
    setView(v => {
      const next = clampScale(v.scale * factor)
      return {
        scale: next,
        tx: v.tx + (v.scale - next) * (focalRelX - originX),
        ty: v.ty + (v.scale - next) * (focalRelY - originY)
      }
    })
  }

  // Zoom about the image center (used by the +/- buttons and reset).
  const zoomFromCenter = (factor: number) => {
    const el = imgRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    setAnimate(true)
    zoomAt(factor, rect.width / 2, rect.height / 2, rect.width / 2, rect.height / 2)
  }

  const resetZoom = () => {
    setAnimate(true)
    setView({ scale: 1, tx: 0, ty: 0 })
  }

  // Wheel zoom must be a non-passive native listener so preventDefault stops
  // the page/dialog from scrolling underneath the image.
  useEffect(() => {
    if (!open) return
    const el = imgRef.current
    if (!el) return

    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const rect = el.getBoundingClientRect()
      const factor = event.deltaY < 0 ? WHEEL_STEP : 1 / WHEEL_STEP
      setAnimate(false)
      zoomAt(factor, event.clientX - rect.left, event.clientY - rect.top, rect.width / 2, rect.height / 2)
    }

    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [open])

  const onPointerDown = (event: ReactPointerEvent<HTMLImageElement>) => {
    ;(event.currentTarget as Element).setPointerCapture?.(event.pointerId)
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY })
    moved.current = false
    setAnimate(false)

    if (pointers.current.size === 1) {
      drag.current = { x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY }
      pinch.current = null
    } else if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()]
      pinch.current = {
        dist: Math.hypot(a.x - b.x, a.y - b.y),
        midX: (a.x + b.x) / 2,
        midY: (a.y + b.y) / 2
      }
      drag.current = null
    }
  }

  const onPointerMove = (event: ReactPointerEvent<HTMLImageElement>) => {
    if (!pointers.current.has(event.pointerId)) return
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY })

    const el = imgRef.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const originX = rect.width / 2
    const originY = rect.height / 2

    if (pointers.current.size >= 2 && pinch.current) {
      const [a, b] = [...pointers.current.values()]
      const dist = Math.hypot(a.x - b.x, a.y - b.y)
      const midX = (a.x + b.x) / 2
      const midY = (a.y + b.y) / 2
      const factor = dist / (pinch.current.dist || dist)

      setView(v => {
        const next = clampScale(v.scale * factor)
        const fx = midX - rect.left
        const fy = midY - rect.top
        return {
          scale: next,
          tx: v.tx + (midX - pinch.current!.midX) + (v.scale - next) * (fx - originX),
          ty: v.ty + (midY - pinch.current!.midY) + (v.scale - next) * (fy - originY)
        }
      })

      pinch.current = { dist, midX, midY }
      moved.current = true
      return
    }

    if (pointers.current.size === 1 && drag.current) {
      const dx = event.clientX - drag.current.x
      const dy = event.clientY - drag.current.y

      if (Math.hypot(event.clientX - drag.current.startX, event.clientY - drag.current.startY) > DRAG_THRESHOLD) {
        moved.current = true
        if (view.scale > 1) setGrabbing(true)
      }

      if (view.scale > 1) {
        setView(v => ({ ...v, tx: v.tx + dx, ty: v.ty + dy }))
      }

      drag.current = { ...drag.current, x: event.clientX, y: event.clientY }
    }
  }

  const onPointerUp = (event: ReactPointerEvent<HTMLImageElement>) => {
    ;(event.currentTarget as Element).releasePointerCapture?.(event.pointerId)
    pointers.current.delete(event.pointerId)
    setGrabbing(false)

    if (pointers.current.size === 1) {
      // Lifting one finger of a pinch continues as a single-finger pan.
      const [only] = [...pointers.current.values()]
      drag.current = { x: only.x, y: only.y, startX: only.x, startY: only.y }
      moved.current = true
      pinch.current = null
    } else if (pointers.current.size === 0) {
      drag.current = null
      pinch.current = null
    }
  }

  const onImageClick = () => {
    // A pan/pinch gesture must not close the lightbox; only a clean click.
    if (!moved.current) onOpenChange(false)
  }

  const cursor = view.scale > 1 ? (grabbing ? 'grabbing' : 'grab') : 'zoom-out'

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent
        className="block w-auto max-h-[calc(100vh-12rem)] max-w-[calc(100vw-12rem)] overflow-visible border-0 bg-transparent p-0 shadow-none"
        showCloseButton={false}
      >
        <div className="group/lightbox relative inline-block">
          <img
            ref={imgRef}
            alt={alt ?? ''}
            className={cn(
              'block max-h-[calc(100vh-12rem)] max-w-[calc(100vw-12rem)] select-none rounded-lg object-contain shadow-2xl',
              animate && 'transition-transform duration-150 ease-out',
              grabbing && 'cursor-grabbing'
            )}
            onClick={onImageClick}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            style={{
              cursor,
              transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})`,
              transformOrigin: 'center center',
              touchAction: 'none'
            }}
            src={src}
          />
          <ImageActionButton
            className="group-hover/lightbox:opacity-100"
            copy={copy}
            onClick={onClick}
            saving={saving}
          />
        </div>
      </DialogContent>

      {/* Zoom controls — fixed to the viewport so they stay put while the
          image is panned/zoomed. stopPropagation keeps them from starting a
          pan or closing the lightbox. Only mounted while the lightbox is open. */}
      {open && (
        <div
          className="fixed bottom-4 left-1/2 z-50 flex -translate-x-1/2 items-center gap-1 rounded-full border border-border/70 bg-background/85 p-1 shadow-lg backdrop-blur"
          onPointerDown={event => event.stopPropagation()}
          onClick={event => event.stopPropagation()}
        >
        <button
          aria-label={copy.zoomOut}
          className="grid size-8 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
          disabled={view.scale <= MIN_SCALE}
          onClick={() => zoomFromCenter(1 / BUTTON_STEP)}
          title={copy.zoomOut}
          type="button"
        >
          <ZoomOut className="size-4" />
        </button>
        <button
          aria-label={copy.resetZoom}
          className="min-w-14 rounded-full px-2 text-center text-xs font-medium tabular-nums text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
          onClick={resetZoom}
          title={copy.resetZoom}
          type="button"
        >
          {Math.round(view.scale * 100)}%
        </button>
        <button
          aria-label={copy.zoomIn}
          className="grid size-8 place-items-center rounded-full text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:opacity-50"
          disabled={view.scale >= MAX_SCALE}
          onClick={() => zoomFromCenter(BUTTON_STEP)}
          title={copy.zoomIn}
          type="button"
        >
          <ZoomIn className="size-4" />
        </button>
        </div>
      )}
    </Dialog>
  )
}

export function ImageActionButton({
  className,
  copy,
  onClick,
  saving
}: {
  className?: string
  copy: ImageActionCopy
  onClick: () => void
  saving: boolean
}) {
  return (
    <button
      aria-label={saving ? copy.savingImage : copy.downloadImage}
      className={cn(
        'absolute right-2 top-2 grid size-8 place-items-center rounded-full border border-border/70 bg-background/80 text-muted-foreground opacity-0 shadow-sm backdrop-blur transition-opacity hover:bg-accent hover:text-foreground focus-visible:opacity-100 disabled:opacity-50',
        className
      )}
      disabled={saving}
      onClick={event => {
        event.stopPropagation()
        void onClick()
      }}
      title={saving ? copy.savingImage : copy.downloadImage}
      type="button"
    >
      <Download className={cn('size-4', saving && 'animate-pulse')} />
    </button>
  )
}
