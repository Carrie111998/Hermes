/**
 * HUD ⇄ app-window handoff.
 *
 * The gateway binds a session's event stream to exactly ONE socket — the last
 * one to submit or resume it (`session["transport"]`). The HUD is a full
 * renderer with its own socket, so entering HUD mode moves that binding to the
 * HUD and the app window stops hearing the session entirely: no deltas, no
 * turn-complete, no draft clear. Nothing to poll for either, since mid-turn
 * there is nothing persisted to re-pull.
 *
 * So leaving HUD mode is a re-home, not a window close. The app window resumes
 * the session the HUD ended on — the existing hydration path, which rebinds the
 * transport, reconciles the transcript, and picks up an in-flight turn — and
 * repaints its composer from the shared draft stash the HUD has been writing.
 */

import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'

import { $composerNewChatGeneration, reloadPersistedDrafts, requestComposerDraftSync } from '@/store/composer'
import { reportHudSession, watchHudState } from '@/store/hud'
import { $primarySessionOwnerIntent, $selectedStoredSessionId } from '@/store/session'
import type { SessionOwnerRoute } from '@/store/session-request-router'
import { $sessionTiles, focusOpenSession, sessionTileDelegate, sessionTileIdentity } from '@/store/session-states'
import { isHudWindow, windowSessionOwnerRoute } from '@/store/windows'

import { getActiveComposer } from '../chat/composer/focus'
import { openSession, type OpenSessionNavigate } from '../open-session'
import { NEW_CHAT_ROUTE, sessionRoute } from '../routes'

import { resolveHudCloseHandoff } from './handoff-target'

/** Session tiles route on `tile:<storedSessionId>` (see session-tile.tsx). */
const TILE_TARGET_PREFIX = 'tile:'

/**
 * The conversation HUD mode should open on: whichever chat surface the user is
 * actually looking at.
 *
 * `$selectedStoredSessionId` is the WORKSPACE pane's session, so reading it
 * alone sent the main tab into the HUD no matter which tile was fronted — the
 * tabs exist precisely so that isn't the same question. `getActiveComposer()`
 * already answers it for the focus bus, healing to the visible surface when its
 * cached claim is buried or gone, and a tile's routing key IS its stored
 * session id.
 */
export function hudTargetSessionId(): null | string {
  return hudTargetSession().sessionId
}

export interface HudOpenTarget {
  ownerRoute?: SessionOwnerRoute
  sessionId: null | string
}

export function hudTargetSession(): HudOpenTarget {
  const target = getActiveComposer()
  const tileIdentity = target.startsWith(TILE_TARGET_PREFIX) ? target.slice(TILE_TARGET_PREFIX.length) : null

  const tile = tileIdentity
    ? $sessionTiles
        .get()
        .find(candidate => sessionTileIdentity(candidate.storedSessionId, candidate.ownerRoute) === tileIdentity)
    : undefined

  if (tile) {
    return { ownerRoute: tile.ownerRoute, sessionId: tile.storedSessionId }
  }

  const sessionId = $selectedStoredSessionId.get()
  const intent = $primarySessionOwnerIntent.get()

  return {
    ...(sessionId && intent?.storedSessionId === sessionId ? { ownerRoute: intent.ownerRoute } : {}),
    sessionId
  }
}

interface HudHandoffParams {
  navigate: OpenSessionNavigate
  resumeSession: (storedSessionId: string, replaceRoute?: boolean, ownerRoute?: SessionOwnerRoute) => unknown
}

/** App-window side: take the session back when the HUD goes away. Also keeps
 *  the titlebar toggle honest when the HUD is closed from its own side. */
export function useHudHandoff({ navigate, resumeSession }: HudHandoffParams): void {
  const paramsRef = useRef<HudHandoffParams>({ navigate, resumeSession })
  paramsRef.current = { navigate, resumeSession }

  useEffect(() => {
    // The HUD's own renderer mounts the same wiring; it is the window going
    // away, so it has nothing to re-home.
    if (isHudWindow()) {
      return
    }

    return watchHudState(hudState => {
      const selected = $selectedStoredSessionId.get()
      const handoff = resolveHudCloseHandoff(hudState, selected)

      // A generation makes null an exact New Chat identity. Adopt the whole
      // main-surface identity before repainting: a stale selected session,
      // exact-owner intent, or route would keep the composer scoped to the chat
      // that was visible before HUD opened and hydrate the wrong draft.
      if (handoff.newChatGeneration !== null) {
        $composerNewChatGeneration.set(handoff.newChatGeneration)
        $selectedStoredSessionId.set(null)
        $primarySessionOwnerIntent.set(null)
        paramsRef.current.navigate(NEW_CHAT_ROUTE)

        // Store + router publications schedule the main composer scope swap.
        // Hydrate on the next microtask so its live scope ref already names
        // this exact generation rather than the previously selected session.
        queueMicrotask(() => {
          reloadPersistedDrafts()
          requestComposerDraftSync('reload')
        })

        return
      }

      // The HUD may have typed or sent since this window last read the stash.
      reloadPersistedDrafts()

      const target = handoff.sessionId

      const workspaceScope = handoff.ownerRoute
        ? { ownerRoute: handoff.ownerRoute, workspaceMode: 'sessions' as const }
        : undefined

      const selectedOwnerIntent = $primarySessionOwnerIntent.get()

      const selectedIdentity = selected
        ? sessionTileIdentity(
            selected,
            selectedOwnerIntent?.storedSessionId === selected ? selectedOwnerIntent.ownerRoute : undefined
          )
        : null

      const targetIdentity = target ? sessionTileIdentity(target, handoff.ownerRoute ?? undefined) : null

      // A raw id is not a session identity when two owners expose it. Route the
      // exact HUD owner through the ordinary main-open path so it publishes the
      // new primary owner intent and lets route hydration bind that backend.
      // Ownerless legacy reports keep the historical same-raw-id shortcut.
      if (target && target === selected && handoff.ownerRoute && targetIdentity !== selectedIdentity) {
        openSession(target, paramsRef.current.navigate, 'main', {
          ownerRoute: handoff.ownerRoute,
          workspaceMode: 'sessions'
        })

        return
      }

      // Somewhere other than the workspace pane. If it is an open tile, front
      // it and re-resume THROUGH the tile delegate: the ordinary resume path
      // enforces "a session is either main or a tile, never both" and would
      // close the tile to take it into main, quietly rearranging tabs the user
      // opened on purpose. Otherwise it's a session this window has never seen
      // — route to it and let the route resume do the rest, including loading
      // its draft as the composer's scope swaps.
      if (target && target !== selected) {
        const delegate =
          (workspaceScope ? focusOpenSession(target, workspaceScope) : focusOpenSession(target)) === 'tile'
            ? sessionTileDelegate()
            : null

        if (delegate) {
          void (
            handoff.ownerRoute
              ? delegate.resumeTile(target, { ownerRoute: handoff.ownerRoute })
              : delegate.resumeTile(target)
          ).catch(() => undefined)

          return
        }

        if (workspaceScope) {
          openSession(target, paramsRef.current.navigate, 'in-place', workspaceScope)
        } else {
          openSession(target, paramsRef.current.navigate)
        }

        return
      }

      // Same session, so the composer's scope never changes and its
      // per-session swap effect will never re-consult the stash. Repaint it.
      requestComposerDraftSync('reload')

      if (target) {
        void paramsRef.current.resumeSession(target, false, handoff.ownerRoute ?? undefined)
      }
    })
  }, [])
}

/** HUD side: follow a retarget. Asking for HUD mode from another tab while the
 *  HUD is already up switches the conversation showing in it. */
export function useHudGoto(navigate: OpenSessionNavigate): void {
  const navigateRef = useRef(navigate)
  navigateRef.current = navigate

  useEffect(
    () =>
      window.hermesDesktop?.hud?.onGoto?.(target => {
        if (typeof target === 'string') {
          navigateRef.current(sessionRoute(target))

          return
        }

        openSession(target.sessionId, navigateRef.current, 'main', {
          ...(target.ownerRoute ? { ownerRoute: target.ownerRoute } : {}),
          workspaceMode: 'sessions'
        })
      }),
    []
  )
}

/** HUD side: keep main told which session this window is on. */
export function useReportHudSession(): void {
  const selectedStoredSessionId = useStore($selectedStoredSessionId)
  const newChatGeneration = useStore($composerNewChatGeneration)
  const ownerRoute = windowSessionOwnerRoute()

  useEffect(() => {
    if (isHudWindow()) {
      reportHudSession(selectedStoredSessionId, newChatGeneration, ownerRoute)
    }
  }, [newChatGeneration, ownerRoute, selectedStoredSessionId])
}
