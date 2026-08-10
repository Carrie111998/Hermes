import { useStore } from '@nanostores/react'
import { useEffect, useMemo } from 'react'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { useI18n } from '@/i18n'
import { Loader2, Lock } from '@/lib/icons'
import { sessionCompacting } from '@/store/compaction'

interface CompactionGuardProps {
  sessionId: null | string
}

/**
 * A controlled, non-dismissible modal tied to the gateway's structured
 * compaction lifecycle. It intentionally has no timer-based escape hatch: the
 * UI unlocks only after the backend clears the session's compacting state.
 */
export function CompactionGuard({ sessionId }: CompactionGuardProps) {
  const compactingStore = useMemo(() => sessionCompacting(sessionId), [sessionId])
  const compacting = useStore(compactingStore)

  useEffect(() => {
    if (!compacting) {
      return
    }

    const blockKeyboardShortcuts = (event: KeyboardEvent) => {
      event.preventDefault()
      event.stopImmediatePropagation()
    }

    window.addEventListener('keydown', blockKeyboardShortcuts, { capture: true })

    return () => window.removeEventListener('keydown', blockKeyboardShortcuts, { capture: true })
  }, [compacting])
  const { t } = useI18n()

  return (
    <Dialog open={compacting}>
      <DialogContent
        className="max-w-md"
        data-compaction-guard=""
        onEscapeKeyDown={event => event.preventDefault()}
        onInteractOutside={event => event.preventDefault()}
        showCloseButton={false}
      >
        <DialogHeader>
          <DialogTitle icon={Lock}>{t.desktop.compactionGuardTitle}</DialogTitle>
          <DialogDescription>{t.desktop.compactionGuardDescription}</DialogDescription>
        </DialogHeader>

        <div
          aria-live="assertive"
          className="flex items-start gap-2.5 rounded-md border border-primary/25 bg-primary/8 px-3 py-2.5 text-[0.75rem] text-foreground"
          role="status"
        >
          <Loader2 className="mt-0.5 size-3.5 shrink-0 animate-spin text-primary" />
          <span>{t.desktop.compactionGuardStatus}</span>
        </div>
      </DialogContent>
    </Dialog>
  )
}
