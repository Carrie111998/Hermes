import type { ComponentProps } from 'react'
import { memo } from 'react'

import { cn } from '@/lib/utils'

export type StatusTone = 'good' | 'muted' | 'warn' | 'bad'

const TONE_BG: Record<StatusTone, string> = {
  good: 'bg-primary',
  muted: 'bg-muted-foreground/40',
  warn: 'bg-amber-500',
  bad: 'bg-destructive'
}

// A quiet breath for the "good" tone — a soft opacity pulse that reads as
// "alive and healthy" without a distracting outward ping ring (gateway-menu
// can stack two or three `good` dots; a chorus of pinging rings would clutter
// the compact panel). Mirrors the working/background dot pattern in
// SessionStatusDot so the two primitives never disagree about what "alive"
// looks like.
//
// Reduced-motion: the blanket override in styles.css
// (animation-duration: 0.01ms !important) neutralizes the pulse for users who
// ask for stillness. The `good` tone still reads as "online" via its brighter
// `bg-primary` background — no motion fallback needed (cf. quest-glow L745,
// which needs a box-shadow fallback because its warning signal is the shadow).
// Matches the spirit of #47942: don't strip the signal with the animation.
// See `@keyframes status-dot-breath` in styles.css.
const BREATH_GOOD = 'status-dot-breath'

interface StatusDotProps extends ComponentProps<'span'> {
  tone: StatusTone
}

export const StatusDot = memo(function StatusDot({ className, tone, ...props }: StatusDotProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'relative inline-block size-1.5 rounded-full',
        TONE_BG[tone],
        tone === 'good' && BREATH_GOOD,
        className
      )}
      {...props}
    />
  )
})
