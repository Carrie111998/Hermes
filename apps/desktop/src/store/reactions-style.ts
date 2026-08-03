/**
 * Message reactions style — off, emoji (current tapbacks), or ambient (continuous color trace).
 *
 * Ambient mode replaces discrete emoji picks with a clickable gradient strip
 * on the message edge — see `message-trace.tsx` and #74981.
 *
 * Presentation-scoped, so the renderer owns it (desktop AGENTS.md).
 * Gated: when 'off', the UI shows no reaction affordances and the agent's
 * react_to_message tool is suppressed via `display.message_reactions` config.
 */

import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'
import { activeGateway } from '@/store/gateway'

export type ReactionsStyle = 'ambient' | 'emoji' | 'off'

const KEY = 'hermes.desktop.reactions-style.v1'

const VALID_STYLES = new Set<string>(['ambient', 'emoji', 'off'])

function storedStyle(): ReactionsStyle {
  const raw = storedString(KEY)

  return raw != null && VALID_STYLES.has(raw) ? (raw as ReactionsStyle) : 'off'
}

export const $reactionsStyle = atom<ReactionsStyle>(typeof window === 'undefined' ? 'off' : storedStyle())

export function setReactionsStyle(style: ReactionsStyle): void {
  $reactionsStyle.set(style)
}

if (typeof window !== 'undefined') {
  $reactionsStyle.listen(style => {
    persistString(KEY, style)
    // Mirror into gateway config: when emoji or ambient, the agent's
    // react_to_message tool is enabled; when off, it's suppressed.
    void activeGateway()
      ?.request('config.set', { key: 'display.message_reactions', value: style === 'off' ? 'false' : 'true' })
      .catch(() => {
        // Not connected yet — the next toggle still holds.
      })
  })
}
