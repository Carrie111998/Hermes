import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import type { ModelPricing } from '@/types/hermes'

// Compact In/Out $/Mtok price tag, mirroring the CLI picker's price columns.
// Shows sale badges and struck-through pre-discount ("was") prices when the
// gateway reports a discount. Renders nothing when pricing is unavailable.
//
// Shared by every model-picking surface (composer catalog menu, standalone
// model picker, onboarding) so pricing can never drift between them.
export function ModelPrice({ price, isCurrent }: { price?: ModelPricing; isCurrent: boolean }) {
  const { t } = useI18n()
  const copy = t.modelPicker

  if (!price || (!price.input && !price.output)) {
    return null
  }

  if (price.free) {
    return (
      <span className="shrink-0 inline-flex items-center gap-1.5">
        {typeof price.discount_percent === 'number' ? (
          <span
            className={cn(
              'rounded-sm px-1 py-0.5 text-[0.62rem] font-semibold',
              isCurrent ? 'bg-primary-foreground/20' : 'bg-amber-500/15 text-amber-700 dark:text-amber-400'
            )}
          >
            -{price.discount_percent}%
          </span>
        ) : null}
        <span
          className={cn(
            'shrink-0 rounded-sm px-1 py-0.5 text-[0.62rem] font-semibold uppercase tracking-wide',
            isCurrent ? 'bg-primary-foreground/20' : 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
          )}
        >
          {copy.free}
        </span>
      </span>
    )
  }

  const onSale = typeof price.discount_percent === 'number' && Boolean(price.was_input || price.was_output)

  return (
    <span
      className={cn(
        'shrink-0 inline-flex items-center gap-1.5 text-[0.66rem] tabular-nums',
        isCurrent ? 'text-primary-foreground/80' : 'text-muted-foreground'
      )}
      title={copy.priceTitle}
    >
      {onSale ? (
        <span
          className={cn(
            'rounded-sm px-1 py-0.5 text-[0.62rem] font-semibold',
            isCurrent ? 'bg-primary-foreground/20' : 'bg-amber-500/15 text-amber-700 dark:text-amber-400'
          )}
        >
          -{price.discount_percent}%
        </span>
      ) : null}
      <span>
        {price.input || '?'} / {price.output || '?'}
      </span>
      {onSale ? (
        <span
          className={cn(
            'line-through decoration-from-font opacity-70',
            isCurrent ? 'text-primary-foreground/60' : 'text-muted-foreground/80'
          )}
        >
          {copy.wasPrice} {price.was_input || '?'} / {price.was_output || '?'}
        </span>
      ) : null}
    </span>
  )
}
