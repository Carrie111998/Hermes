import { useAuiState } from '@assistant-ui/react'
import { type FC } from 'react'

import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { formatMessageTimestamp } from './timestamp'

/**
 * Always-visible message time — "Today, 3:42 PM", "Yesterday, 3:42 PM", or an
 * absolute date+time for older messages (see {@link formatMessageTimestamp}).
 *
 * Distinct from the hover-only relative age in the assistant action bar
 * (`MessageAge`): that one is compact and sits behind an opacity-0 hover
 * reveal, so there was no way to see *when* a message was written at a glance.
 * This is the durable "when did this happen" answer on every message.
 *
 * Renders nothing while the timestamp is missing (pending / streaming rows)
 * or unparseable.
 */
export const MessageTimestamp: FC<{ className?: string }> = ({ className }) => {
  const { t } = useI18n()
  const createdAt = useAuiState(s => s.message.createdAt)
  const label = formatMessageTimestamp(createdAt, t.assistant.thread)

  if (!label) {
    return null
  }

  return (
    <span
      className={cn('text-[0.6875rem] tabular-nums text-muted-foreground/70', className)}
      data-slot="aui_msg-timestamp"
    >
      {label}
    </span>
  )
}
