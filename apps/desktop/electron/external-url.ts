// Protocols that Desktop may hand to the OS via shell.openExternal (or the
// WSL→Windows `cmd /c start` path). Kept as an explicit allowlist so chat
// markdown can't drive arbitrary custom handlers (javascript:, data:, ms-*,
// etc.). `file:` is intentionally NOT here — openExternalUrl routes it through
// shell.openPath after path hardening instead.
//
// App deep-link schemes go here only when:
//   1. a real product surface wants them clickable in chat, and
//   2. the handler is a known local desktop app (not a web navigation).
// Obsidian vault deep links (`obsidian://open?vault=…&file=…`) are the first.

export const SHELL_OPEN_EXTERNAL_PROTOCOLS = new Set([
  'http:',
  'https:',
  'mailto:',
  'obsidian:'
])

/** True when `rawUrl` parses and its protocol is on the shell-open allowlist. */
export function isAllowedShellOpenExternalUrl(rawUrl: unknown): boolean {
  const raw = String(rawUrl ?? '').trim()

  if (!raw) {
    return false
  }

  let parsed: URL

  try {
    parsed = new URL(raw)
  } catch {
    return false
  }

  return SHELL_OPEN_EXTERNAL_PROTOCOLS.has(parsed.protocol.toLowerCase())
}
