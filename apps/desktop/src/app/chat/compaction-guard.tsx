import { useStore } from '@nanostores/react'
import { useEffect, useId, useMemo, useRef } from 'react'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { useI18n } from '@/i18n'
import { Loader2, Lock } from '@/lib/icons'
import { sessionCompacting } from '@/store/compaction'

interface CompactionGuardProps {
  sessionId: null | string
  sessionLabel: string
}

/**
 * A controlled, non-dismissible overlay contained by one positioned chat
 * surface. Pointer and keyboard input cannot reach that session while the
 * gateway rewrites its context, but the surrounding Desktop (sidebar and
 * other panes) remains available.
 */
export function CompactionGuard({ sessionId, sessionLabel }: CompactionGuardProps) {
  const compactingStore = useMemo(() => sessionCompacting(sessionId), [sessionId])
  const compacting = useStore(compactingStore)
  const paneVisible = usePaneVisible()
  const guardRef = useRef<HTMLDivElement>(null)
  const titleId = useId()
  const descriptionId = useId()
  const { t } = useI18n()

  useEffect(() => {
    if (compacting && paneVisible) {
      guardRef.current?.focus({ preventScroll: true })
    }
  }, [compacting, paneVisible])

  if (!compacting) {
    return null
  }

  const shortSessionId = sessionId ? sessionId.slice(0, 8) : 'unknown'

  return (
    <div
      aria-describedby={descriptionId}
      aria-labelledby={titleId}
      className="absolute inset-0 z-50 grid place-items-center overflow-auto bg-(--ui-chat-surface-background) px-6 py-10"
      data-compaction-guard=""
      data-session-id={sessionId || ''}
      onKeyDown={event => {
        event.preventDefault()
        event.stopPropagation()
      }}
      ref={guardRef}
      role="dialog"
      tabIndex={-1}
    >
      <div className="w-full max-w-md rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary) p-5 shadow-xl">
        <div className="flex items-start gap-3">
          <div className="grid size-8 shrink-0 place-items-center rounded-md bg-primary/12 text-primary">
            <Lock className="size-4" />
          </div>
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-foreground" id={titleId}>
              {t.desktop.compactionGuardTitle(sessionLabel)}
            </h2>
            <p className="mt-1 font-mono text-[0.6875rem] text-(--ui-text-tertiary)">
              {t.desktop.compactionGuardSessionId(shortSessionId)}
            </p>
            <p className="mt-2 text-[0.75rem] leading-relaxed text-(--ui-text-secondary)" id={descriptionId}>
              {t.desktop.compactionGuardDescription}
            </p>
          </div>
        </div>

        <div
          aria-live="assertive"
          className="mt-4 flex items-start gap-2.5 rounded-md border border-primary/25 bg-primary/8 px-3 py-2.5 text-[0.75rem] text-foreground"
          role="status"
        >
          <Loader2 className="mt-0.5 size-3.5 shrink-0 animate-spin text-primary" />
          <span>{t.desktop.compactionGuardStatus}</span>
        </div>
      </div>
    </div>
  )
}
