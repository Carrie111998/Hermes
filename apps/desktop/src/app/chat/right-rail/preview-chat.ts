/**
 * PREVIEW ↔ CHAT BINDING — which session `read_preview` should treat as
 * the owner of the Browser tab.
 *
 * The Browser is a singleton (`url:browser`). Without a pin, `read_preview`
 * reads whatever tab is in front. A pin lets the user say "this page is for
 * that chat": when THAT session asks, the reader prefers the Browser tab
 * even if a file peek is currently showing. Other sessions keep the
 * look-at-the-front-tab behavior.
 *
 * Pin is a live runtime id (matches `preview.read.request`'s session_id).
 * Not persisted — runtime ids die on restart.
 */

import { atom } from 'nanostores'

import { sessionTitle } from '@/lib/chat-runtime'
import { $activeSessionId, $selectedStoredSessionId, $sessions } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

export interface PreviewChatChoice {
  id: string
  kind: 'primary' | 'tile'
  label: string
}

export const $previewChat = atom<string | null>(null)

export function setPreviewChat(id: string | null): void {
  $previewChat.set(id)
}

export function chatChoices(): PreviewChatChoice[] {
  const sessions = $sessions.get()
  const primaryRuntime = $activeSessionId.get()
  const primaryStored = $selectedStoredSessionId.get()
  const tiles = $sessionTiles.get()
  const rows: PreviewChatChoice[] = []

  if (primaryRuntime) {
    const stored = primaryStored ? sessions.find(session => session.id === primaryStored) : undefined

    rows.push({
      id: primaryRuntime,
      kind: 'primary',
      label: stored ? sessionTitle(stored) : 'Chat'
    })
  }

  for (const tile of tiles) {
    if (!tile.runtimeId || tile.runtimeId === primaryRuntime) {
      continue
    }

    const stored = sessions.find(session => session.id === tile.storedSessionId)

    rows.push({
      id: tile.runtimeId,
      kind: 'tile',
      label: stored ? sessionTitle(stored) : tile.storedSessionId
    })
  }

  return rows
}
