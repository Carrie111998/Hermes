import { useStore } from '@nanostores/react'
import { App } from '@capacitor/app'
import { Keyboard } from '@capacitor/keyboard'
import { useEffect } from 'react'
import { useLocation } from 'react-router'

import { requestComposerAttachFiles, requestComposerInsert } from '@/app/chat/composer/focus'
import { PANE_TOGGLE_REVEAL_EVENT } from '@/components/pane-shell'
import { $previewTabs, closeRightRail } from '@/store/preview'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'
import {
  $fileBrowserOpen,
  $panesFlipped,
  $sidebarOpen,
  CHAT_SIDEBAR_PANE_ID,
  FILE_BROWSER_PANE_ID,
  setFileBrowserOpen,
  setSidebarOpen,
} from '@/store/layout'

import {
  mobileDrawerForPane,
  shouldDismissDrawerAfterSessionChange,
  shouldRevealPaneForDrawerChange,
  shouldSuppressPreviewOnMobile,
} from './mobile-policy'
import { consumePendingInboundShare, listenForInboundShare } from '~bridge/inbound-share'

/**
 * MobileBehaviors — the touch adaptations layered over the reused desktop UI.
 *
 *  1. Sidebar drawers: the titlebar burgers flip $sidebarOpen/$fileBrowserOpen,
 *     but collapsed panes only show via PANE_TOGGLE_REVEAL_EVENT (the mod+B
 *     path). Bridge that, normalize the pane flip, and dismiss on tap-outside.
 *  2. One Android back handler: keyboard → dismiss, then overlay → close, then
 *     drawer → close, else history back / exit.
 *
 * Overlay screens (Settings/Skills/Profiles) get their responsive master-detail
 * from upstream now (overlays/overlay-split-layout.tsx), so nothing here.
 */
function revealPane(id: string, mode: 'close' | 'open' | 'toggle' = 'toggle') {
  window.dispatchEvent(new CustomEvent(PANE_TOGGLE_REVEAL_EVENT, { detail: { id, mode } }))
}
function anyDrawerOpen() {
  return $sidebarOpen.get() || $fileBrowserOpen.get()
}
function closeOpenDrawer() {
  if ($sidebarOpen.get()) setSidebarOpen(false)
  else if ($fileBrowserOpen.get()) setFileBrowserOpen(false)
}

function dismissTopOverlay(): boolean {
  if (!document.querySelector('[data-overlay-surface]')) return false

  window.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, cancelable: true, key: 'Escape' }))
  return true
}

export function MobileBehaviors() {
  const location = useLocation()
  const sidebarOpen = useStore($sidebarOpen)

  // Navigating (selecting a session) dismisses an open chat drawer.
  useEffect(() => {
    if (shouldDismissDrawerAfterSessionChange(anyDrawerOpen())) closeOpenDrawer()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname])

  // A share is an explicit Android user action. Keep its text in the draft and
  // stage its files as ordinary composer attachments; never auto-send content
  // into a remote agent session just because another app opened Hermes.
  useEffect(() => {
    let disposed = false
    let stopListening: () => void = () => undefined

    const consumeShare = async () => {
      const share = await consumePendingInboundShare()
      if (disposed) return

      if (share.text) requestComposerInsert(share.text, { mode: 'block' })
      if (share.files.length) requestComposerAttachFiles(share.files)
    }

    void consumeShare()
    void listenForInboundShare(() => void consumeShare()).then(stop => {
      if (disposed) stop()
      else stopListening = stop
    })

    return () => {
      disposed = true
      stopListening()
    }
  }, [])

  useEffect(() => {
    // Standard orientation: sessions LEFT, files RIGHT.
    $panesFlipped.set(false)

    // Track keyboard visibility for Android Back only. Chromium already owns
    // IME viewport layout; programmatic scrollIntoView on every resize creates
    // a resize → scroll → resize feedback loop on Fold-class devices.
    let keyboardOpen = false
    const kbShow = Keyboard.addListener('keyboardWillShow', () => {
      keyboardOpen = true
    })
    const kbHide = Keyboard.addListener('keyboardWillHide', () => {
      keyboardOpen = false
    })

    const offSidebar = $sidebarOpen.listen(open => {
      revealPane(CHAT_SIDEBAR_PANE_ID, shouldRevealPaneForDrawerChange(open) ? 'open' : 'close')
    })
    const offFiles = $fileBrowserOpen.listen(open => {
      revealPane(FILE_BROWSER_PANE_ID, shouldRevealPaneForDrawerChange(open) ? 'open' : 'close')
    })

    // NarrowOverlays owns the visual drawer, while layout owns the state behind
    // titlebar buttons and Android Back. Keep both directions synchronized so
    // the overlay's own X never leaves an invisible drawer marked as open.
    const onPaneReveal = (event: Event) => {
      const detail = (event as CustomEvent<{ id?: string; mode?: 'close' | 'open' | 'toggle' }>).detail
      if (detail?.mode !== 'close') return

      const drawer = mobileDrawerForPane(detail.id)
      if (drawer === 'sessions') setSidebarOpen(false)
      else if (drawer === 'files') setFileBrowserOpen(false)
    }
    window.addEventListener(PANE_TOGGLE_REVEAL_EVENT, onPaneReveal)

    const syncDrawerAttr = () => {
      document.documentElement.toggleAttribute('data-drawer-open', anyDrawerOpen())
    }
    const dismissDrawerForSessionChange = () => {
      if (shouldDismissDrawerAfterSessionChange(anyDrawerOpen())) closeOpenDrawer()
    }
    const dismissPhonePreview = () => {
      if (shouldSuppressPreviewOnMobile(window.innerWidth, $previewTabs.get().length)) closeRightRail()
    }
    const offSidebarAttr = $sidebarOpen.subscribe(syncDrawerAttr)
    const offFilesAttr = $fileBrowserOpen.subscribe(syncDrawerAttr)
    const offActiveSession = $activeSessionId.listen(dismissDrawerForSessionChange)
    const offStoredSession = $selectedStoredSessionId.listen(dismissDrawerForSessionChange)
    const offPreview = $previewTabs.listen(dismissPhonePreview)
    dismissPhonePreview()

    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Element | null

      // Tap outside an open chat drawer → dismiss it.
      if (anyDrawerOpen() && !target?.closest('[data-narrow-pane-overlay]')) {
        e.stopPropagation()
        closeOpenDrawer()
      }
    }
    document.addEventListener('pointerdown', onPointerDown, true)

    let disposed = false
    let backHandle: { remove: () => void } | undefined
    void App.addListener('backButton', ({ canGoBack }) => {
      // Keyboard open → just dismiss it (and blur, so it doesn't auto-reopen
      // from the composer regaining focus). Don't navigate.
      if (keyboardOpen) {
        ;(document.activeElement as HTMLElement | null)?.blur()
        void Keyboard.hide()
        return
      }
      if (dismissTopOverlay()) {
        return
      }
      if (anyDrawerOpen()) {
        closeOpenDrawer()
        return
      }
      if (canGoBack) window.history.back()
      else void App.exitApp()
    }).then((handle) => {
      if (disposed) {
        void handle.remove()
      } else {
        backHandle = handle
      }
    })

    return () => {
      disposed = true
      offSidebar()
      offFiles()
      offSidebarAttr()
      offFilesAttr()
      offActiveSession()
      offStoredSession()
      offPreview()
      window.removeEventListener(PANE_TOGGLE_REVEAL_EVENT, onPaneReveal)
      document.removeEventListener('pointerdown', onPointerDown, true)
      backHandle?.remove()
      void kbShow.then((h) => h.remove())
      void kbHide.then((h) => h.remove())
      document.documentElement.removeAttribute('data-drawer-open')
    }
  }, [])

  const toggleSessionDrawer = () => {
    setSidebarOpen(!sidebarOpen)
  }

  // Desktop titlebar controls can be intentionally absent on the native shell.
  // Keep a permanent, thumb-sized session entry point instead of making Android
  // Back the only way to recover the session drawer.
  return (
    <button
      aria-label={sidebarOpen ? 'Close sessions' : 'Open sessions'}
      className="mobile-session-drawer-trigger"
      onClick={toggleSessionDrawer}
      type="button"
    >
      ☰
    </button>
  )
}
