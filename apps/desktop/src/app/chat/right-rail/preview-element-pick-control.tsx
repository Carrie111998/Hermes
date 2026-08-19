/**
 * Element-pick toggle for the in-app browser bar.
 *
 * Same control as ZCode's "Select page element for chat": click to enter
 * pick mode, click again (or Escape) to cancel. PreviewPane injects the
 * guest picker and inserts the snapshot into the composer.
 */

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

export function PreviewElementPickControl({ onToggle, picking }: { onToggle: () => void; picking: boolean }) {
  const { t } = useI18n()
  const copy = t.preview.web

  return (
    <Tip label={picking ? copy.pickCancel : copy.pick}>
      <Button
        aria-label={picking ? copy.pickCancel : copy.pick}
        aria-pressed={picking || undefined}
        className={cn(
          'self-center bg-transparent select-none',
          picking ? 'opacity-100' : 'opacity-60 hover:opacity-100'
        )}
        onClick={onToggle}
        onPointerDown={event => event.stopPropagation()}
        size="icon-xs"
        type="button"
        variant="ghost"
      >
        <Codicon name="inspect" size="0.8125rem" />
      </Button>
    </Tip>
  )
}
