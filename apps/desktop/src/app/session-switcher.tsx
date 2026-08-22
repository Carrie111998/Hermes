import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router'

import { sessionTitle } from '@/lib/chat-runtime'
import { cn } from '@/lib/utils'
import { $sessionDotStateById } from '@/store/session-dot-state'
import { $switcherIndex, $switcherOpen, $switcherSessions, closeSwitcher } from '@/store/session-switcher'

import { SessionStatusDot } from './chat/session-status-dot'
import { HUD_ITEM, HUD_POSITION, HUD_SURFACE, HUD_TEXT } from './floating-hud'
import { openSession } from './open-session'

// Compact session-switcher HUD — keyboard-driven from `use-keybinds`, rows
// clickable via mousedown (Ctrl+click on macOS). No Dialog: Tab stays global.
export function SessionSwitcher() {
  const open = useStore($switcherOpen)
  const sessions = useStore($switcherSessions)
  const index = useStore($switcherIndex)
  // The same reduced status the row dot paints, so the switcher's outline
  // agrees with the sidebar's: a blocking prompt outlines the row here too.
  const dotStates = useStore($sessionDotStateById)
  const navigate = useNavigate()

  const activeRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'nearest' })
  }, [index, open])

  if (!open || sessions.length === 0) {
    return null
  }

  const pick = (sessionId: string) => {
    closeSwitcher()
    openSession(sessionId, navigate)
  }

  return createPortal(
    <>
      {/* Transparent click-catcher: click-away closes, but no dim/blur. */}
      <div
        className="fixed inset-0 z-(--z-switcher-backdrop)"
        onMouseDown={e => {
          e.preventDefault()
          closeSwitcher()
        }}
      />
      <div
        className={cn(
          HUD_POSITION,
          HUD_SURFACE,
          'dt-portal-scrollbar z-(--z-switcher) max-h-[min(22rem,64vh)] w-[min(19rem,calc(100vw-2rem))] select-none overflow-y-auto p-1'
        )}
      >
        {sessions.map((session, i) => {
          const selected = i === index

          return (
            <div
              className={cn(
                'row-hover flex items-center rounded leading-tight',
                HUD_ITEM,
                HUD_TEXT,
                selected ? 'bg-accent text-accent-foreground' : 'text-(--ui-text-secondary)',
                // A blocking prompt outlines the row in the highlight-picker
                // color — same treatment as the sidebar row, same source.
                dotStates[session.id] === 'needs-input' && 'ring-1 ring-inset ring-(--dt-primary)'
              )}
              key={session.id}
              onMouseDown={e => {
                e.preventDefault()
                pick(session.id)
              }}
              ref={selected ? activeRef : undefined}
            >
              <SessionStatusDot className="shrink-0" session={session} storedSessionId={session.id} />
              <span className="min-w-0 flex-1 truncate">{sessionTitle(session)}</span>
              {i < 9 && (
                <span
                  className={cn(
                    'shrink-0 font-mono text-[0.625rem] tabular-nums',
                    selected ? 'text-accent-foreground/70' : 'text-(--ui-text-quaternary)'
                  )}
                >
                  ⌃{i + 1}
                </span>
              )}
            </div>
          )
        })}
      </div>
    </>,
    document.body
  )
}
