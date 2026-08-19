import { useEffect, useState } from 'react'

import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

// Braille spinner frames — reads as a tiny ASCII loader in monospace.
const FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

export interface ChatSwapError {
  profile: string
  message: string
}

// Shown over the conversation while the live gateway swaps to another profile's
// backend (lazily spawned). Keeps the last profile name through the fade-out so
// the label doesn't blank. Purely visual — pointer-events-none. When a switch
// FAILS (descriptor lookup or dial), the overlay renders the surfaced error in
// place of the spinner so a dead click is never silent.
export function ChatSwapOverlay({
  error,
  profile
}: {
  error: ChatSwapError | null
  profile: string | null
}) {
  const { t } = useI18n()
  const [frame, setFrame] = useState(0)
  const [label, setLabel] = useState<null | string>(profile)

  useEffect(() => {
    if (profile) {
      setLabel(profile)
    }
  }, [profile])

  useEffect(() => {
    if (!profile) {
      return
    }

    const id = window.setInterval(() => setFrame(value => (value + 1) % FRAMES.length), 80)

    return () => window.clearInterval(id)
  }, [profile])

  if (error) {
    return (
      <div
        aria-hidden
        className={cn(
          'pointer-events-none absolute inset-0 z-50 flex items-center justify-center transition-opacity duration-150 ease-out',
          'opacity-100'
        )}
      >
        <div
          className="flex max-w-md items-start gap-2 bg-[color-mix(in_srgb,var(--dt-card)_92%,transparent)] px-4 py-2 font-mono text-[0.8125rem] text-destructive shadow-composer"
          role="alert"
        >
          <span aria-hidden className="mt-0.5 shrink-0 text-(--ui-accent)">
            ⚠
          </span>
          <span className="min-w-0 break-words">
            {t.composer.switchFailed(error.profile)}
            <span className="opacity-80"> — {error.message}</span>
          </span>
        </div>
      </div>
    )
  }

  return (
    <div
      aria-hidden
      className={cn(
        'pointer-events-none absolute inset-0 z-50 flex items-center justify-center transition-opacity duration-150 ease-out',
        profile ? 'opacity-100' : 'opacity-0'
      )}
    >
      <div className="flex items-center gap-2 bg-[color-mix(in_srgb,var(--dt-card)_92%,transparent)] px-4 py-2 font-mono text-[0.8125rem] text-foreground shadow-composer">
        <span className="w-3 text-(--ui-accent)">{FRAMES[frame]}</span>
        {t.composer.wakingProfile(label ?? '')}
      </div>
    </div>
  )
}
