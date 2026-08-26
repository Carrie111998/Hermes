import { useStore } from '@nanostores/react'
import { App } from '@capacitor/app'
import { Keyboard } from '@capacitor/keyboard'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'

import { COMMAND_CENTER_ROUTE, NEW_CHAT_ROUTE, SETTINGS_ROUTE } from '@/app/routes'
import { hudTargetSessionId } from '@/app/hud/handoff'
import { requestComposerAttachFiles, requestComposerInsert } from '@/app/chat/composer/focus'
import { toggleLayoutEditMode } from '@/components/pane-shell/edit-mode'
import { allPaneIds } from '@/components/pane-shell/tree/model'
import { $hiddenTreePanes, $layoutTree } from '@/components/pane-shell/tree/store'
import { PANE_TOGGLE_REVEAL_EVENT } from '@/components/pane-shell'
import { useTitlebarToolContributions } from '@/app/contrib/panes'
import { useContributions } from '@/contrib/react/use-contributions'
import { useI18n } from '@/i18n'
import { hostPathLabel } from '@/lib/external-link'
import { openCommandPalette } from '@/store/command-palette'
import { $hapticsMuted, toggleHapticsMuted } from '@/store/haptics'
import { toggleHud } from '@/store/hud'
import { $previewTabs, closeRightRailTab, newBrowserTab, openPreview } from '@/store/preview'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'
import {
  $fileBrowserOpen,
  $panesFlipped,
  $rightRailActiveTabId,
  $sidebarOpen,
  CHAT_SIDEBAR_PANE_ID,
  FILE_BROWSER_PANE_ID,
  selectRightRailTab,
  setFileBrowserOpen,
  setSidebarOpen,
  togglePanesFlipped,
} from '@/store/layout'

import { mobileWorkspacePanes } from './mobile-workspace-menu'
import { MobilePreviewOverlay } from './mobile-preview-overlay'
import { shouldOpenMobilePreview } from './mobile-preview-policy'
import { MobileToolbar, type MobileToolbarAction } from './mobile-toolbar'
import { mobileToolbarContextActions, type MobileToolbarContextAction } from './mobile-toolbar-model'
import {
  mobileDrawerForEdgeSwipe,
  mobileDrawerForPane,
  shouldCloseMobileDrawerFromSwipe,
  shouldDismissDrawerAfterSessionChange,
  shouldRevealPaneForDrawerChange,
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
  const navigate = useNavigate()
  const { t } = useI18n()
  const sidebarOpen = useStore($sidebarOpen)
  const tree = useStore($layoutTree)
  const hiddenPanes = useStore($hiddenTreePanes)
  const panes = useContributions('panes')
  const previewTabs = useStore($previewTabs)
  const activePreviewTabId = useStore($rightRailActiveTabId)
  const [mobilePreviewOpen, setMobilePreviewOpen] = useState(false)
  const mobilePreviewOpenRef = useRef(false)
  mobilePreviewOpenRef.current = mobilePreviewOpen
  const previewTabsRef = useRef(previewTabs)
  const previewTabsInitializedRef = useRef(false)
  const [workspaceMenuOpen, setWorkspaceMenuOpen] = useState(false)
  const workspaceMenuOpenRef = useRef(false)
  workspaceMenuOpenRef.current = workspaceMenuOpen
  const workspacePanes = useMemo(
    () => mobileWorkspacePanes(panes, new Set(tree ? allPaneIds(tree) : []), hiddenPanes),
    [hiddenPanes, panes, tree],
  )
  const hapticsMuted = useStore($hapticsMuted)
  const leftTitlebarTools = useTitlebarToolContributions('left')
  const rightTitlebarTools = useTitlebarToolContributions('right')
  const appActions = useMemo<MobileToolbarAction[]>(
    () => [
      { id: 'new-chat', label: t.commandCenter.nav.newChat.title, onSelect: () => navigate(NEW_CHAT_ROUTE) },
      {
        id: 'preview',
        label: t.preview.openPreview,
        onSelect: () => {
          if (!previewTabs.length) newBrowserTab()
          setMobilePreviewOpen(true)
        },
      },
      { id: 'command-center', label: t.commandCenter.commandCenter, onSelect: () => navigate(COMMAND_CENTER_ROUTE) },
      { id: 'command-palette', label: t.commandCenter.paletteTitle, onSelect: openCommandPalette },
      { id: 'settings', label: t.titlebar.openSettings, onSelect: () => navigate(SETTINGS_ROUTE) },
      { id: 'layout', label: t.titlebar.layoutEditor, onSelect: toggleLayoutEditMode },
      { id: 'hud', label: t.titlebar.enterHud, onSelect: () => toggleHud(hudTargetSessionId()) },
      {
        id: 'haptics',
        label: hapticsMuted ? t.titlebar.unmuteHaptics : t.titlebar.muteHaptics,
        onSelect: toggleHapticsMuted,
      },
      { id: 'flip-panes', label: t.titlebar.swapSidebarSides, onSelect: togglePanesFlipped },
      {
        id: 'keybinds',
        label: t.settings.nav.keybinds,
        onSelect: () => window.dispatchEvent(new CustomEvent('hermes:open-keybinds')),
      },
    ],
    [hapticsMuted, navigate, previewTabs.length, t],
  )
  const runContributedToolbarTool = useCallback(
    (tool: MobileToolbarContextAction) => {
      if (tool.disabled) return

      if (tool.href) {
        void window.hermesDesktop.openExternal(tool.href)
        return
      }
      if (tool.to) {
        navigate(tool.to)
        return
      }
      tool.onSelect?.()
    },
    [navigate],
  )
  const contributedToolbarTools = useMemo(
    () =>
      mobileToolbarContextActions([...leftTitlebarTools, ...rightTitlebarTools]).map(tool => ({
        ...tool,
        onSelect: () => runContributedToolbarTool(tool),
      })),
    [leftTitlebarTools, rightTitlebarTools, runContributedToolbarTool],
  )

  // Navigating (selecting a session) dismisses an open chat drawer.
  useEffect(() => {
    if (shouldDismissDrawerAfterSessionChange(anyDrawerOpen())) closeOpenDrawer()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname])

  useEffect(() => {
    document.documentElement.toggleAttribute('data-mobile-toolbar-menu-open', workspaceMenuOpen)
    return () => document.documentElement.removeAttribute('data-mobile-toolbar-menu-open')
  }, [workspaceMenuOpen])

  useEffect(() => {
    const previous = previewTabsRef.current

    if (previewTabsInitializedRef.current) {
      if (previewTabs.length === 0) {
        setMobilePreviewOpen(false)
      } else if (shouldOpenMobilePreview(previous, previewTabs)) {
        setMobilePreviewOpen(true)
      }
    } else {
      previewTabsInitializedRef.current = true
    }

    previewTabsRef.current = previewTabs
  }, [previewTabs])

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
    const offSidebarAttr = $sidebarOpen.subscribe(syncDrawerAttr)
    const offFilesAttr = $fileBrowserOpen.subscribe(syncDrawerAttr)
    const offActiveSession = $activeSessionId.listen(dismissDrawerForSessionChange)
    const offStoredSession = $selectedStoredSessionId.listen(dismissDrawerForSessionChange)

    let swipeStart: { insideDrawer: boolean; x: number; y: number } | null = null
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Element | null
      if (workspaceMenuOpenRef.current) return
      const insideDrawer = Boolean(target?.closest('[data-narrow-pane-overlay]'))

      // Tap outside an open chat drawer → dismiss it.
      if (anyDrawerOpen() && !insideDrawer) {
        e.stopPropagation()
        closeOpenDrawer()

        return
      }

      // Never turn a modal/overlay interaction into a drawer gesture.
      if (e.pointerType !== 'touch' || document.querySelector('[data-overlay-surface]')) return
      swipeStart = { insideDrawer, x: e.clientX, y: e.clientY }
    }
    const onPointerUp = (e: PointerEvent) => {
      const start = swipeStart
      swipeStart = null

      if (!start || e.pointerType !== 'touch' || document.querySelector('[data-overlay-surface]')) return
      const deltaX = e.clientX - start.x
      const deltaY = e.clientY - start.y

      if (anyDrawerOpen()) {
        const drawer = $sidebarOpen.get() ? 'sessions' : 'files'
        if (start.insideDrawer && shouldCloseMobileDrawerFromSwipe(drawer, deltaX, deltaY)) closeOpenDrawer()

        return
      }

      const drawer = mobileDrawerForEdgeSwipe({
        endX: e.clientX,
        endY: e.clientY,
        startX: start.x,
        startY: start.y,
        viewportWidth: window.innerWidth
      })
      if (drawer === 'sessions') setSidebarOpen(true)
      else if (drawer === 'files') setFileBrowserOpen(true)
    }
    document.addEventListener('pointerdown', onPointerDown, true)
    document.addEventListener('pointerup', onPointerUp, true)

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
      if (mobilePreviewOpenRef.current) {
        setMobilePreviewOpen(false)
        return
      }
      if (workspaceMenuOpenRef.current) {
        setWorkspaceMenuOpen(false)
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
      window.removeEventListener(PANE_TOGGLE_REVEAL_EVENT, onPaneReveal)
      document.removeEventListener('pointerdown', onPointerDown, true)
      document.removeEventListener('pointerup', onPointerUp, true)
      backHandle?.remove()
      void kbShow.then((h) => h.remove())
      void kbHide.then((h) => h.remove())
      document.documentElement.removeAttribute('data-drawer-open')
    }
  }, [])

  const toggleSessionDrawer = () => {
    setSidebarOpen(!sidebarOpen)
  }

  const openWorkspacePane = (id: string) => {
    const drawer = mobileDrawerForPane(id)
    if (drawer === 'sessions') {
      setFileBrowserOpen(false)
      setSidebarOpen(true)
      return
    }
    if (drawer === 'files') {
      setSidebarOpen(false)
      setFileBrowserOpen(true)
      return
    }

    closeOpenDrawer()
    revealPane(id, 'open')
  }

  // The mobile shell owns one compact toolbar. Sessions gets a direct familiar
  // hamburger + left-edge swipe; every other Desktop control lives in one
  // ordered overflow sheet rather than becoming its own permanent rail.
  return (
    <>
      <MobileToolbar
        appActions={appActions}
        contextActions={contributedToolbarTools}
        menuOpen={workspaceMenuOpen}
        onClose={() => setWorkspaceMenuOpen(false)}
        onOpenSessions={toggleSessionDrawer}
        onToggleMenu={() => setWorkspaceMenuOpen(open => !open)}
        onWorkspacePane={openWorkspacePane}
        sessionsOpen={sidebarOpen}
        workspacePanes={workspacePanes}
      />
      <MobilePreviewOverlay
        activeTabId={activePreviewTabId}
        onClose={() => setMobilePreviewOpen(false)}
        onCloseTab={tabId => {
          closeRightRailTab(tabId)
          if (previewTabs.length <= 1) setMobilePreviewOpen(false)
        }}
        onNavigate={url => openPreview({ kind: 'url', label: hostPathLabel(url), source: url, url }, 'manual')}
        onNewBrowserTab={() => {
          newBrowserTab()
          setMobilePreviewOpen(true)
        }}
        onOpenExternal={url => void window.hermesDesktop.openExternal(url)}
        onSelectTab={selectRightRailTab}
        open={mobilePreviewOpen}
        tabs={previewTabs}
      />
    </>
  )
}
