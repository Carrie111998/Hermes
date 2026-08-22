import { $currentCwd } from '@/store/session'

import { $chatTerminalRunRequest, setTerminalTakeover, takeChatTerminalRunRequest } from '../store'

import { createTerminal } from './terminals'

// Eligibility guard, not a shell-safety classifier. Clicking Run is explicit
// user authorization for the visible command; this only rejects text whose
// hidden/control bytes could differ materially from what the user approved.
function isUnsafeTerminalDisplayCodeUnit(code: number): boolean {
  return (
    (code >= 0x00 && code <= 0x08) ||
    (code >= 0x0b && code <= 0x1f) ||
    (code >= 0x7f && code <= 0x9f) ||
    code === 0x061c ||
    (code >= 0x200b && code <= 0x200f) ||
    (code >= 0x2028 && code <= 0x202e) ||
    code === 0x2060 ||
    (code >= 0x2066 && code <= 0x2069) ||
    code === 0xfeff
  )
}

function hasUnsafeTerminalDisplayChars(command: string): boolean {
  for (let index = 0; index < command.length; index += 1) {
    if (isUnsafeTerminalDisplayCodeUnit(command.charCodeAt(index))) {
      return true
    }
  }

  return false
}

// Interactive PTY injection is intentionally bounded; oversized fences remain copy-only.
export const MAX_CHAT_RUN_CHARS = 32_768

export function isRunnableChatTerminalCommandText(command: string): boolean {
  return Boolean(command.trim()) && command.length <= MAX_CHAT_RUN_CHARS && !hasUnsafeTerminalDisplayChars(command)
}

export function hasEmbeddedTerminalBridge(): boolean {
  const terminal = typeof window === 'undefined' ? undefined : window.hermesDesktop?.terminal

  return typeof terminal?.start === 'function' && typeof terminal.write === 'function'
}

/** Deliver one exact-terminal request. Authorization is consumed before the PTY side effect. */
export function deliverChatTerminalRunRequest(
  terminalId: string,
  ptySessionId: string,
  write: (ptySessionId: string, data: string) => Promise<boolean>
): boolean {
  const command = takeChatTerminalRunRequest(terminalId)

  if (!command) {
    return false
  }

  void write(ptySessionId, `${command}\r`)

  return true
}

/** Queue a user-approved command into a brand-new user shell, never an existing
 * SSH/REPL/TUI/agent tab. Only one not-yet-flushed chat command may exist. */
export function queueChatCommandInFreshTerminal(command: string): string | null {
  if (!hasEmbeddedTerminalBridge() || !isRunnableChatTerminalCommandText(command) || $chatTerminalRunRequest.get()) {
    return null
  }

  const terminalId = createTerminal($currentCwd.get())
  setTerminalTakeover(true)
  $chatTerminalRunRequest.set({ command, terminalId })

  return terminalId
}
