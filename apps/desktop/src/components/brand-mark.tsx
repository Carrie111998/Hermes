import { cn } from '@/lib/utils'

// Brand badge: typographic placeholder ("DA" on emerald) until real Douglas
// Agent iconography exists. Deliberately no external image -- the previous
// mark was Nous Research's own illustrated mascot (nous-girl.jpg), which MIT
// covers as code but not as artwork; it can't represent a different product.
// Identical in light/dark; size via className (default size-14).
export function BrandMark({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      className={cn(
        'font-display inline-flex size-14 shrink-0 items-center justify-center overflow-hidden rounded-md bg-emerald-600 font-bold text-white select-none',
        className
      )}
      {...props}
    >
      <span aria-hidden className="text-[0.4em] tracking-tight">
        DA
      </span>
      <span className="sr-only">Douglas Agent</span>
    </span>
  )
}
