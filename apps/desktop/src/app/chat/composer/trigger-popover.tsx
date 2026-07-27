import type { Unstable_TriggerItem } from '@assistant-ui/core'
import { Fragment, useEffect, useRef } from 'react'

import { Codicon } from '@/components/ui/codicon'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { COMPLETION_DRAWER_BELOW_CLASS, COMPLETION_DRAWER_CLASS, CompletionDrawerEmpty } from './completion-drawer'

const AT_ICON_BY_TYPE: Record<string, string> = {
  diff: 'diff',
  file: 'book',
  folder: 'folder',
  git: 'git-branch',
  image: 'file-media',
  simple: 'symbol-misc',
  staged: 'diff-added',
  tool: 'tools',
  url: 'globe'
}

function atIcon(item: Unstable_TriggerItem) {
  const meta = item.metadata as { rawText?: string } | undefined
  const raw = meta?.rawText || item.label

  if (raw.startsWith('@diff')) {
    return AT_ICON_BY_TYPE.diff
  }

  if (raw.startsWith('@staged')) {
    return AT_ICON_BY_TYPE.staged
  }

  return AT_ICON_BY_TYPE[item.type] || AT_ICON_BY_TYPE.simple
}

interface RowMeta {
  display?: string
  group?: string
  meta?: string
}

const ROW_BASE_CLASS = [
  'relative flex w-full cursor-default select-none rounded-md px-2 py-1 text-left',
  'outline-hidden transition-colors hover:bg-(--ui-bg-tertiary)',
  'data-[highlighted]:bg-(--ui-bg-tertiary) data-[highlighted]:text-foreground'
].join(' ')

interface ComposerTriggerPopoverProps {
  activeIndex: number
  items: readonly Unstable_TriggerItem[]
  kind: '@' | '/'
  loading: boolean
  onHover: (index: number) => void
  onPick: (item: Unstable_TriggerItem) => void
  placement?: 'bottom' | 'top'
}

export function ComposerTriggerPopover({
  activeIndex,
  items,
  kind,
  loading,
  onHover,
  onPick,
  placement = 'top'
}: ComposerTriggerPopoverProps) {
  const { t } = useI18n()
  const copy = t.composer
  const isSlash = kind === '/'
  const listRef = useRef<HTMLDivElement>(null)
  // Index of the row the mouse last highlighted. Hover-driven highlights must
  // not scroll (the row is already under the cursor, and a nudge would move
  // rows under the pointer and re-fire hover in a loop) — only keyboard
  // navigation may drive the drawer's scroll position.
  const hoverIndexRef = useRef(-1)

  useEffect(() => {
    if (activeIndex === hoverIndexRef.current) {
      return
    }

    const list = listRef.current

    if (!list) {
      return
    }

    // At the top, pin the drawer to 0 so the first group header stays visible;
    // `nearest` alone would stop at the row and leave the header clipped.
    if (activeIndex <= 0) {
      list.scrollTop = 0

      return
    }

    list.querySelector('[data-highlighted]')?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex, items])

  let lastGroup: string | undefined

  return (
    <div
      className={placement === 'bottom' ? COMPLETION_DRAWER_BELOW_CLASS : COMPLETION_DRAWER_CLASS}
      data-slot="composer-completion-drawer"
      data-state="open"
      onMouseDown={event => event.preventDefault()}
      onMouseLeave={() => {
        hoverIndexRef.current = -1
      }}
      ref={listRef}
      role="listbox"
    >
      {items.length === 0 ? (
        loading ? (
          <div className="flex items-center gap-2 px-2 py-1.5 text-(--ui-text-tertiary)">
            <GlyphSpinner ariaLabel={copy.lookupLoading} className="text-foreground/70" spinner="braille" />
            <span>{copy.lookupLoading}</span>
          </div>
        ) : (
          <CompletionDrawerEmpty title={copy.lookupNoMatches}>
            {kind === '@' ? (
              <>
                {copy.lookupTry} <span className="font-mono text-foreground/80">@file:</span> {copy.lookupOr}{' '}
                <span className="font-mono text-foreground/80">@folder:</span>.
              </>
            ) : (
              <>
                {copy.lookupTry} <span className="font-mono text-foreground/80">/help</span>.
              </>
            )}
          </CompletionDrawerEmpty>
        )
      ) : (
        items.map((item, index) => {
          const meta = item.metadata as RowMeta | undefined
          const display = meta?.display ?? (isSlash ? `/${item.label}` : item.label)
          const description = meta?.meta || item.description
          const group = meta?.group?.trim()
          const showHeader = isSlash && Boolean(group) && group !== lastGroup
          const isFirstHeader = lastGroup === undefined
          lastGroup = group || lastGroup
          const active = index === activeIndex

          return (
            <Fragment key={item.id}>
              {showHeader && (
                <div
                  className={cn(
                    'select-none px-2 pb-0.5 text-[0.625rem] font-semibold uppercase tracking-wider text-(--ui-text-tertiary)',
                    isFirstHeader ? 'pt-0.5' : 'pt-2'
                  )}
                >
                  {group}
                </div>
              )}
              <button
                className={cn(ROW_BASE_CLASS, isSlash ? 'flex-col gap-0' : 'items-center gap-2')}
                data-highlighted={active ? '' : undefined}
                onClick={() => onPick(item)}
                onMouseEnter={() => {
                  hoverIndexRef.current = index
                  onHover(index)
                }}
                type="button"
              >
                {isSlash ? (
                  <>
                    {/* Active row (keyboard nav or hover) un-truncates inline so
                        long command names / descriptions stay readable without a
                        floating tooltip. */}
                    <span
                      className={cn(
                        'font-medium leading-snug text-foreground',
                        active ? 'whitespace-normal break-words' : 'truncate'
                      )}
                    >
                      {display}
                    </span>
                    {description && (
                      <span
                        className={cn(
                          'leading-snug text-(--ui-text-tertiary)',
                          active ? 'whitespace-normal break-words' : 'truncate'
                        )}
                      >
                        {description}
                      </span>
                    )}
                  </>
                ) : (
                  <>
                    <span className="grid size-4 shrink-0 place-items-center text-(--ui-text-tertiary)">
                      <Codicon name={atIcon(item)} size="0.875rem" />
                    </span>
                    <span className="min-w-0 shrink truncate font-mono font-medium leading-5 text-foreground">
                      {display}
                    </span>
                    {description && (
                      <span className="min-w-0 flex-1 truncate leading-5 text-(--ui-text-tertiary)">{description}</span>
                    )}
                  </>
                )}
              </button>
            </Fragment>
          )
        })
      )}
    </div>
  )
}
