/**
 * Chat picker for the in-app browser toolbar.
 *
 * Lives beside the URL field. Picks which session `read_preview` should
 * treat as the owner of the Browser tab. See preview-chat.ts.
 */

import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { TooltipIconButton } from '@/components/assistant-ui/tooltip-icon-button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useI18n } from '@/i18n'
import { MessageSquareText } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { $previewChat, chatChoices, setPreviewChat } from './preview-chat'

export function PreviewChatControl() {
  const { t } = useI18n()
  const copy = t.preview.web
  const pinned = useStore($previewChat)
  const [open, setOpen] = useState(false)
  const choices = chatChoices()

  const pick = (id: string | null) => {
    setPreviewChat(id)
    setOpen(false)
  }

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverTrigger asChild>
        <TooltipIconButton tooltip={copy.chat} type="button">
          <MessageSquareText className={cn(pinned && 'text-primary')} />
        </TooltipIconButton>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-56 p-1.5" side="bottom">
        <div aria-label={copy.chat} className="flex flex-col gap-0.5" role="listbox">
          <button
            className={cn(
              'flex w-full items-center justify-between rounded-sm px-2 py-1 text-left text-xs',
              pinned === null ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/60'
            )}
            onClick={() => pick(null)}
            role="option"
            type="button"
          >
            <span>{copy.chatNone}</span>
          </button>
          {choices.map(choice => (
            <button
              className={cn(
                'flex w-full items-center justify-between rounded-sm px-2 py-1 text-left text-xs',
                pinned === choice.id ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/60'
              )}
              key={choice.id}
              onClick={() => pick(choice.id)}
              role="option"
              type="button"
            >
              <span className="min-w-0 truncate">{choice.label}</span>
              {choice.kind === 'tile' && <span className="shrink-0 text-muted-foreground">{copy.chatTile}</span>}
            </button>
          ))}
          {choices.length === 0 && (
            <div className="px-2 py-1 text-[0.625rem] text-muted-foreground">{copy.chatEmpty}</div>
          )}
        </div>
      </PopoverContent>
    </Popover>
  )
}
