import { useStore } from '@nanostores/react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { ModelMenuCloseContext } from '@/app/shell/model-menu-panel'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { releaseTypingFocus } from '@/components/ui/keyboard-first'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { ChevronDown } from '@/lib/icons'
import { modelOptionsQueryKey } from '@/lib/model-options'
import { formatModelStatusLabel } from '@/lib/model-status-label'
import { cn } from '@/lib/utils'
import { $activeGatewayProfile } from '@/store/profile'
import { $currentModelSource, $defaultReasoningEffort, setModelPickerOpen } from '@/store/session'
import type { ModelOptionsResponse } from '@/types/hermes'

import { onComposerModelMenuRequest } from './focus'
import { useComposerScope } from './scope'
import type { ChatBarState } from './types'

// `shrink` (not `shrink-0`) with a truncating label: the pill is the one
// control in the row that can give width back continuously, so it absorbs the
// squeeze between collapse stages instead of pushing Send past the edge.
const PILL = cn(
  'h-(--composer-control-size) min-w-0 max-w-40 shrink gap-1 rounded-md px-2 text-xs font-normal',
  'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'
)

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
  const copy = useI18n().t.shell.statusbar
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

  // #96063: subscribe to the profile-scoped model-options cache so a config
  // default change re-paints the pill with the new baseline. `enabled: false`
  // keeps it a pure cache subscription — no fetch, no spinner; the catalog
  // fetcher (model-menu-panel / ComposerControls) owns that.
  const profile = useStore($activeGatewayProfile)

  const defaultProvider = useQuery<ModelOptionsResponse | undefined>({
    enabled: false,
    queryFn: () => undefined,
    queryKey: modelOptionsQueryKey(profile)
  }).data?.provider?.trim() ?? ''

  // Two same-named models on two providers (e.g. `qwen3.7-plus` on both
  // `custom:aliyun-coding-plan` and `custom:token-plan-a`) would otherwise look
  // identical. When the live provider has drifted from the Settings default,
  // paint a muted tag inside the pill so the desync is visible at a glance
  // — and extend the tooltip / aria-label so hover + screen readers both name
  // both providers.
  const liveProvider = currentProvider.trim()
  const hasDefaultProvider = defaultProvider.length > 0
  const showProviderTag = hasDefaultProvider && liveProvider.length > 0 && liveProvider !== defaultProvider

  const label = compact ? (
    <ChevronDown className="size-3.5 shrink-0 opacity-70" />
  ) : (
    <>
      {currentModel.trim() ? (
        <span className="truncate">
          {formatModelStatusLabel(currentModel, { defaultEffort, fastMode, reasoningEffort })}
          {showProviderTag && (
            <span
              aria-hidden="true"
              className="ml-0.5 opacity-70"
              data-testid="model-provider-tag"
            >
              {copy.providerTag(liveProvider)}
            </span>
          )}
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

  // #96063: when the live provider has drifted from the Settings default, name
  // both so hover + screen readers can tell which provider the session will
  // actually route to. The visible provider tag inside the label covers the
  // "glance" path; this carries the same information into the tooltip /
  // aria-label path so users who do hover (or who use AT) get the full picture.
  const nonDefaultSuffix = showProviderTag ? ` — ${copy.nonDefaultProvider(liveProvider, defaultProvider)}` : ''

  const title = `${baseTitle}${nonDefaultSuffix}${pinnedOverride ? ` — ${copy.modelPinned}` : ''}`

  if (!model.modelMenuContent) {
    const pickerLabel = `${copy.openModelPicker}${nonDefaultSuffix}${pinnedOverride ? ` — ${copy.modelPinned}` : ''}`

    return (
      <Tip label={pickerLabel} side="top">
        <Button
          aria-label={pickerLabel}
          className={pillClass}
          disabled={disabled}
          onClick={() => setModelPickerOpen(true)}
          type="button"
          variant="ghost"
        >
          {label}
        </Button>
      </Tip>
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
  )
}
