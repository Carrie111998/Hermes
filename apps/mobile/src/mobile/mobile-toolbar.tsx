import { useEffect, useRef } from 'react'

import { Codicon } from '@/components/ui/codicon'

import type { MobileToolbarContextAction } from './mobile-toolbar-model'
import type { MobileWorkspacePane } from './mobile-workspace-menu'

export interface MobileToolbarAction {
  disabled?: boolean
  id: string
  label: string
  onSelect: () => void
}

export interface MobileToolbarProps {
  appActions: readonly MobileToolbarAction[]
  contextActions: readonly MobileToolbarContextAction[]
  menuOpen: boolean
  onClose: () => void
  onOpenSessions: () => void
  onToggleMenu: () => void
  onWorkspacePane?: (id: string) => void
  sessionsOpen: boolean
  workspacePanes: readonly MobileWorkspacePane[]
}

function runAction(onClose: () => void, action: MobileToolbarAction | MobileToolbarContextAction) {
  if (action.disabled) return
  onClose()
  action.onSelect?.()
}

/**
 * The only persistent mobile navigation chrome: sessions remain one tap/left
 * swipe away; all other desktop actions live in the same bottom sheet.
 */
export function MobileToolbar({
  appActions,
  contextActions,
  menuOpen,
  onClose,
  onOpenSessions,
  onToggleMenu,
  onWorkspacePane,
  sessionsOpen,
  workspacePanes
}: MobileToolbarProps) {
  const overflowButtonRef = useRef<HTMLButtonElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const wasMenuOpen = useRef(menuOpen)

  useEffect(() => {
    if (menuOpen) {
      closeButtonRef.current?.focus()
    } else if (wasMenuOpen.current) {
      overflowButtonRef.current?.focus()
    }

    wasMenuOpen.current = menuOpen
  }, [menuOpen])

  useEffect(() => {
    if (!menuOpen) return

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onClose()
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [menuOpen, onClose])

  return (
    <>
      <div aria-label="Mobile navigation" className="mobile-top-toolbar" role="toolbar">
        <button
          aria-label={sessionsOpen ? 'Hide sidebar' : 'Show sidebar'}
          className="mobile-toolbar-button"
          onClick={onOpenSessions}
          type="button"
        >
          <Codicon name="menu" size="1.35rem" />
        </button>
        <div aria-hidden className="mobile-top-toolbar-center" />
        <button
          aria-expanded={menuOpen}
          aria-haspopup="dialog"
          aria-label="Open desktop actions"
          className="mobile-toolbar-button"
          onClick={onToggleMenu}
          ref={overflowButtonRef}
          type="button"
        >
          <Codicon name="ellipsis" size="1.35rem" />
        </button>
      </div>
      {menuOpen && (
        <>
          <button aria-label="Close desktop actions" className="mobile-toolbar-scrim" onClick={onClose} type="button" />
          <section
            aria-label="Desktop actions"
            aria-modal="true"
            className="mobile-toolbar-menu"
            data-mobile-toolbar-origin="top-right"
            role="dialog"
          >
            <header>
              <span>Hermes</span>
              <button aria-label="Close desktop actions" onClick={onClose} ref={closeButtonRef} type="button">
                <Codicon name="close" size="1.1rem" />
              </button>
            </header>
            <div className="mobile-toolbar-menu-scroll">
              <section aria-label="Workspace" role="group">
                <p>Workspace</p>
                {workspacePanes.map(pane => (
                  <button
                    key={pane.id}
                    onClick={() => {
                      onClose()
                      onWorkspacePane?.(pane.id)
                    }}
                    type="button"
                  >
                    {pane.title}
                  </button>
                ))}
              </section>
              <section aria-label="Interface" role="group">
                <p>Interface</p>
                {appActions.map(action => (
                  <button
                    disabled={action.disabled}
                    key={action.id}
                    onClick={() => runAction(onClose, action)}
                    type="button"
                  >
                    {action.label}
                  </button>
                ))}
              </section>
              {contextActions.length > 0 && (
                <section aria-label="Context actions" role="group">
                  <p>Context</p>
                  {contextActions.map(action => (
                    <button
                      disabled={action.disabled}
                      key={action.id}
                      onClick={() => runAction(onClose, action)}
                      type="button"
                    >
                      {action.label}
                    </button>
                  ))}
                </section>
              )}
            </div>
          </section>
        </>
      )}
    </>
  )
}
