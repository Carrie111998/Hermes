import type { HudSessionState } from '@/store/hud'

export type HudCloseHandoff = HudSessionState

/**
 * Resolve the chat identity the app window must adopt when the HUD closes.
 * A generation makes a null session an exact New Chat identity; legacy null
 * reports without one retain the app window's selected stored-session fallback.
 */
export function resolveHudCloseHandoff(
  hudState: HudSessionState,
  selectedSessionId: string | null
): HudCloseHandoff {
  if (hudState.sessionId) {
    return { newChatGeneration: null, sessionId: hudState.sessionId }
  }

  if (hudState.newChatGeneration !== null) {
    return { newChatGeneration: hudState.newChatGeneration, sessionId: null }
  }

  return { newChatGeneration: null, sessionId: selectedSessionId }
}
