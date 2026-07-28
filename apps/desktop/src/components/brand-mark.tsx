import { cn } from '@/lib/utils'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

// Brand badge: same glyph as the Windows taskbar / PE-stamped Hermes.exe icon
// (apple-touch-icon.png ← assets/icon). Crop the outer letterbox so small
// tiles show the character, not the black pad. Identical in light/dark.
export function BrandMark({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      className={cn(
        'inline-flex size-14 shrink-0 items-center justify-center overflow-hidden rounded-md bg-white',
        className
      )}
      {...props}
    >
      <img
        alt=""
        className="size-[128%] max-w-none object-cover object-center"
        src={assetPath('apple-touch-icon.png')}
      />
    </span>
  )
}
