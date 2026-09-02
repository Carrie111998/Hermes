import { useEffect, useRef, useState } from 'react'

import { composerPanelCard } from '@/components/chat/composer-dock'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import type { HermesGateway } from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'

import { type PromptRewriteMode, requestPromptRewrite } from './prompt-rewrite'

interface PromptRewriteMenuProps {
  cwd?: null | string
  disabled: boolean
  gateway: HermesGateway | null | undefined
  getDraft: () => string
  onRewrite: (text: string) => void
  sessionId?: null | string
}

export function PromptRewriteMenu({ cwd, disabled, gateway, getDraft, onRewrite, sessionId }: PromptRewriteMenuProps) {
  const { t } = useI18n()
  const c = t.composer
  const [rewriting, setRewriting] = useState(false)
  const generationRef = useRef(0)

  useEffect(
    () => () => {
      generationRef.current += 1
    },
    []
  )

  const rewrite = async (mode: PromptRewriteMode) => {
    const snapshot = getDraft()
    const source = snapshot.trim()

    if (!gateway || !source || rewriting) {
      return
    }

    const generation = (generationRef.current += 1)
    setRewriting(true)

    try {
      const result = await requestPromptRewrite({ cwd, gateway, mode, sessionId, text: source })

      if (generation !== generationRef.current) {
        return
      }

      if (!result) {
        throw new Error(c.rewriteFailed)
      }

      // The model call is asynchronous. Never replace newer typing with a
      // rewrite of an older snapshot; keep the user's current draft intact and
      // explain why the completed result was discarded.
      if (getDraft() !== snapshot) {
        notify({ kind: 'info', message: c.rewriteDraftChanged, title: c.rewritePrompt })

        return
      }

      onRewrite(result)
    } catch (error) {
      if (generation === generationRef.current) {
        notifyError(error, c.rewriteFailed)
      }
    } finally {
      if (generation === generationRef.current) {
        setRewriting(false)
      }
    }
  }

  const options: { description: string; icon: string; label: string; mode: PromptRewriteMode }[] = [
    { description: c.rewriteBasicDesc, icon: 'edit', label: c.rewriteBasic, mode: 'basic' },
    { description: c.rewriteBriefDesc, icon: 'list-flat', label: c.rewriteBrief, mode: 'brief' },
    { description: c.rewriteDetailedDesc, icon: 'list-unordered', label: c.rewriteDetailed, mode: 'detailed' },
    { description: c.rewriteEnhanceDesc, icon: 'sparkle', label: c.rewriteEnhance, mode: 'enhance' }
  ]

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={rewriting ? c.rewritingPrompt : c.rewritePrompt}
          className={cn(
            'h-(--composer-control-size) shrink-0 gap-1 rounded-md px-2 text-xs font-normal',
            'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
          )}
          disabled={disabled || rewriting || !gateway}
          type="button"
          variant="ghost"
        >
          <Codicon name={rewriting ? 'loading' : 'edit-sparkle'} size="0.875rem" spinning={rewriting} />
          <span>{c.rewritePrompt}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className={cn('w-72', composerPanelCard)} side="top" sideOffset={6}>
        <DropdownMenuLabel>{c.rewritePrompt}</DropdownMenuLabel>
        {options.map(option => (
          <DropdownMenuItem className="items-start" key={option.mode} onSelect={() => void rewrite(option.mode)}>
            <Codicon className="mt-0.5 shrink-0 opacity-70" name={option.icon} size="0.875rem" />
            <span className="grid min-w-0 gap-0.5">
              <span>{option.label}</span>
              <span className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                {option.description}
              </span>
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
