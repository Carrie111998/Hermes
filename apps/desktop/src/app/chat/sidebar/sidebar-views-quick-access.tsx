import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { $activeSavedSidebarViewId, $savedSidebarViews, applySavedSidebarView } from '@/store/sidebar-views'

const HOVER_CLOSE_DELAY_MS = 180

export function SidebarSavedViewsQuickAccess({ className }: { className?: string }) {
  const { t } = useI18n()
  const savedViews = useStore($savedSidebarViews).views
  const activeViewId = useStore($activeSavedSidebarViewId)
  const [open, setOpen] = useState(false)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const cancelClose = useCallback(() => {
    if (closeTimer.current) {
      clearTimeout(closeTimer.current)
      closeTimer.current = null
    }
  }, [])

  const openNow = () => {
    cancelClose()
    setOpen(true)
  }

  const scheduleClose = () => {
    cancelClose()
    closeTimer.current = setTimeout(() => setOpen(false), HOVER_CLOSE_DELAY_MS)
  }

  useEffect(() => cancelClose, [cancelClose])

  if (savedViews.length === 0) {
    return null
  }

  const label = t.sidebar.viewMenu.savedViews

  return (
    <div className="grid size-6 place-items-center">
      <Popover onOpenChange={setOpen} open={open}>
        <PopoverTrigger asChild>
          <Button
            aria-expanded={open}
            aria-haspopup="dialog"
            aria-label={label}
            className={cn(className, open && 'bg-(--ui-control-active-background) text-foreground opacity-100')}
            onClick={event => event.stopPropagation()}
            onFocus={openNow}
            onPointerEnter={openNow}
            onPointerLeave={scheduleClose}
            size="icon-xs"
            type="button"
            variant="ghost"
          >
            <Codicon name="eye" size="0.75rem" />
          </Button>
        </PopoverTrigger>

        <PopoverContent
          align="start"
          aria-label={label}
          className="w-52 p-1"
          onFocusCapture={cancelClose}
          onPointerEnter={cancelClose}
          onPointerLeave={scheduleClose}
        >
          <div className="px-2 py-1 text-[0.6875rem] font-medium text-(--ui-text-tertiary)">{label}</div>
          {savedViews.map(view => (
            <Button
              aria-label={view.name}
              className="h-7 w-full justify-start gap-2 px-2 font-normal"
              key={view.id}
              onClick={event => {
                event.stopPropagation()
                applySavedSidebarView(view.id)
                setOpen(false)
              }}
              type="button"
              variant="ghost"
            >
              <span className="flex w-3 shrink-0 items-center justify-center">
                {activeViewId === view.id && <Codicon name="check" size="0.75rem" />}
              </span>
              <span className="truncate">{view.name}</span>
            </Button>
          ))}
        </PopoverContent>
      </Popover>
    </div>
  )
}
