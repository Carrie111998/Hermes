import { cn } from '@/lib/utils'

const assetPath = (path: string) => `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`

// Eva's Electric Sheep app portrait. The source asset is the same identity
// used by the managed macOS bundle so in-app and Finder branding stay aligned.
export function BrandMark({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      className={cn(
        'inline-flex size-14 shrink-0 items-center justify-center overflow-hidden rounded-md bg-white',
        className
      )}
      {...props}
    >
      <img alt="" className="size-full object-contain" src={assetPath('eva.png')} />
    </span>
  )
}
