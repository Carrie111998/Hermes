import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { ModelMenuCloseContext } from '@/app/shell/model-menu-panel'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { releaseTypingFocus } from '@/components/ui/keyboard-first'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { compactNumber } from '@/lib/format'
import { ChevronDown } from '@/lib/icons'
import { formatModelStatusLabel } from '@/lib/model-status-label'
import { cn } from '@/lib/utils'
import { $currentModelSource, $defaultReasoningEffort, setModelPickerOpen } from '@/store/session'
import type { UsageStats } from '@/types/hermes'

import { onComposerModelMenuRequest } from './focus'
import { useComposerScope } from './scope'
import type { ChatBarState } from './types'

const PILL = cn(
  'h-(--composer-control-size) max-w-40 shrink-0 gap-1 rounded-md px-2 text-xs font-normal',
  'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
)

/** Returns the remaining model context only when the gateway reported a real window. */
export function contextTokensRemaining(usage: null | UsageStats | undefined): null | number {
  const contextMax = usage?.context_max

  if (typeof contextMax !== 'number' || !Number.isFinite(contextMax) || contextMax <= 0) {
    return null
  }

  const contextUsed = usage?.context_used

  if (typeof contextUsed === 'number' && Number.isFinite(contextUsed)) {
    return Math.max(0, Math.round(contextMax - Math.max(0, contextUsed)))
  }

  const contextPercent = usage?.context_percent

  if (typeof contextPercent !== 'number' || !Number.isFinite(contextPercent)) {
    return null
  }

  const boundedPercent = Math.max(0, Math.min(100, contextPercent))

  return Math.max(0, Math.round(contextMax * (1 - boundedPercent / 100)))
}

/**
 * Composer model selector — the relocated status-bar pill. Reuses the live
 * `model.options` dropdown (`modelMenuContent`) verbatim; falls back to the
 * full picker when the gateway is closed and no live menu exists.
 *
 * Display follows THIS surface's SessionView (primary or tile) — never the
 * primary-only globals — so side-by-side panes each show their own model.
 */
export function ModelPill({
  compact = false,
  disabled,
  model
}: {
  compact?: boolean
  disabled: boolean
  model: ChatBarState['model']
}) {
  const { t } = useI18n()
  const copy = t.shell.statusbar
  const view = useSessionView()
  // Prefer the chat-bar snapshot (already view-scoped by ChatView); fall back
  // to the live SessionView atoms so a mid-flight session.info still paints.
  const viewModel = useStore(view.$model)
  const viewProvider = useStore(view.$provider)
  const currentModel = model.model || viewModel
  const currentProvider = model.provider || viewProvider
  const fastMode = useStore(view.$fast)
  const reasoningEffort = useStore(view.$reasoningEffort)
  const modelSource = useStore($currentModelSource)
  const defaultEffort = useStore($defaultReasoningEffort)
  const runtimeId = useStore(view.$runtimeId)
  const usage = useStore(view.$usage)
  const [open, setOpen] = useState(false)
  const scope = useComposerScope()
  const hasLiveMenu = Boolean(model.modelMenuContent)

  // The `composer.modelPicker` hotkey, routed to exactly one surface (the pane
  // under the pointer, else the active composer — see requestModelMenuToggle).
  // Toggles the live dropdown; with no live menu (gateway closed) it opens the
  // full picker dialog, same as clicking the pill.
  useEffect(
    () =>
      onComposerModelMenuRequest(target => {
        if (target !== scope.target || disabled) {
          return
        }

        if (hasLiveMenu) {
          setOpen(prev => !prev)
        } else {
          setModelPickerOpen(true)
        }
      }),
    [scope.target, disabled, hasLiveMenu]
  )

  // The composer pick is sticky: a manual selection is pinned and every NEW
  // chat uses it instead of the Settings → Model default — silently, which has
  // cost users real money on a forgotten paid-model pick (#62055). Surface the
  // pin whenever a draft (no live session) is running on a manual override. A
  // live session's footer reflects that session's model, so no badge there.
  // Tiles always have a runtime — pin badge is primary-draft only.
  const pinnedOverride =
    view.kind === 'primary' && !runtimeId && modelSource === 'manual' && Boolean(currentModel.trim())

  // The model resolves a beat after the gateway/session comes up. Rather than
  // flash a literal "No model", show a quiet loader (inherits the pill text
  // color at half opacity) until a model lands.
  const label = compact ? (
    <ChevronDown className="size-3.5 shrink-0 opacity-70" />
  ) : (
    <>
      {currentModel.trim() ? (
        <span className="truncate">
          {formatModelStatusLabel(currentModel, { defaultEffort, fastMode, reasoningEffort })}
        </span>
      ) : (
        <GlyphSpinner className="opacity-50" spinner="braille" />
      )}
      {pinnedOverride && (
        <span
          aria-label={copy.modelPinned}
          className="size-1 shrink-0 rounded-full bg-(--ui-accent)"
          data-testid="model-pinned-dot"
          role="img"
        />
      )}
      <ChevronDown className="size-2.5 shrink-0 opacity-50" />
    </>
  )

  // Compact (floating composer): a snug square holding just the chevron — no pill
  // padding, sized to match the other composer icon buttons.
  const pillClass = compact
    ? cn(
        'size-(--composer-control-size) shrink-0 justify-center gap-0 rounded-md p-0',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
      )
    : PILL

  const baseTitle = currentProvider
    ? copy.modelTitle(currentProvider, currentModel || copy.modelNone)
    : copy.switchModel

  const title = pinnedOverride ? `${baseTitle} — ${copy.modelPinned}` : baseTitle
  const remainingContext = compact ? null : contextTokensRemaining(usage)
  const contextMax = usage?.context_max
  const contextLabel = remainingContext === null ? null : t.composer.contextRemaining(compactNumber(remainingContext))

  const contextDetail =
    contextLabel && typeof contextMax === 'number'
      ? t.composer.contextRemainingDetail(compactNumber(remainingContext ?? 0), compactNumber(contextMax))
      : null

  const contextCounter =
    contextLabel && contextDetail ? (
      <Tip label={contextDetail} side="top">
        <span
          aria-label={contextDetail}
          className="inline-flex h-(--composer-control-size) shrink-0 cursor-default items-center rounded-md px-1 text-[0.6875rem] font-medium tabular-nums text-(--ui-text-quaternary)"
          data-testid="context-remaining"
        >
          {contextLabel}
        </span>
      </Tip>
    ) : null

  if (!model.modelMenuContent) {
    return (
      <div className="flex shrink-0 items-center gap-1">
        <Tip label={pinnedOverride ? `${copy.openModelPicker} — ${copy.modelPinned}` : copy.openModelPicker} side="top">
          <Button
            aria-label={copy.openModelPicker}
            className={pillClass}
            disabled={disabled}
            onClick={() => setModelPickerOpen(true)}
            type="button"
            variant="ghost"
          >
            {label}
          </Button>
        </Tip>
        {contextCounter}
      </div>
    )
  }

  // Closing the menu ends its claim on the keyboard: Radix restores focus to
  // this pill (a toolbar button), so without the release the Enter that
  // committed a model also swallows whatever you type next.
  const setMenuOpen = (next: boolean) => {
    setOpen(next)

    if (!next) {
      releaseTypingFocus()
    }
  }

  return (
    <div className="flex shrink-0 items-center gap-1">
      <DropdownMenu onOpenChange={setMenuOpen} open={open}>
        <Tip label={title} side="top">
          <DropdownMenuTrigger asChild>
            <Button aria-label={title} className={pillClass} disabled={disabled} type="button" variant="ghost">
              {label}
            </Button>
          </DropdownMenuTrigger>
        </Tip>
        <DropdownMenuContent align="end" className="w-64 p-0" side="top" sideOffset={8}>
          <ModelMenuCloseContext.Provider value={() => setMenuOpen(false)}>
            {model.modelMenuContent}
          </ModelMenuCloseContext.Provider>
        </DropdownMenuContent>
      </DropdownMenu>
      {contextCounter}
    </div>
  )
}
