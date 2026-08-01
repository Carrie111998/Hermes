import { cn } from '../lib/utils'

import logoWhite from '../assets/brand/logo_white.png'

// Brand badge: the Douglas Agent mark (white line art, transparent
// background) on the same emerald tile the old "DA" placeholder used.
// Identical in light/dark.
export function BrandMark({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      className={cn(
        'inline-flex size-14 shrink-0 items-center justify-center overflow-hidden rounded-md bg-emerald-600 select-none',
        className
      )}
      {...props}
    >
      <img aria-hidden src={logoWhite} alt="" className="size-[70%] object-contain" />
      <span className="sr-only">Douglas Agent</span>
    </span>
  )
}
