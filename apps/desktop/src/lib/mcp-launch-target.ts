/**
 * Launch-target formatting for the MCP consent card.
 *
 * Ported from MoonshotAI/kimi-code#2843: a pre-consent MCP prompt must show
 * WHAT will actually execute (the stdio command line) or be contacted (the
 * remote URL), not just the server's display name — otherwise the user
 * approves a name without seeing the thing they are approving.
 *
 * Config-supplied text is untrusted (a planted config.yaml or a catalog
 * mirror could carry terminal escape sequences), so every rendered target is
 * stripped of control characters and bounded in length before it reaches the
 * card.
 */

export interface McpLaunchSource {
  url?: string | null
  command?: string | null
  args?: string[] | null
}

export interface McpLaunchTarget {
  kind: 'remote' | 'stdio'
  /** Sanitized, length-bounded display string (URL or command line). */
  target: string
}

/** Longest target line the card will render before eliding. */
const MAX_TARGET_LENGTH = 160

// C0 controls (except nothing — the card renders a single line, so tab and
// newline are just as unwelcome), DEL, and the C1 range. Covers raw ESC so a
// crafted `args` entry cannot inject ANSI sequences into the pre-consent UI.
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\u0000-\u001F\u007F-\u009F]/g

function sanitize(text: string): string {
  const clean = text.replace(CONTROL_CHARS, ' ').replace(/\s+/g, ' ').trim()

  return clean.length > MAX_TARGET_LENGTH ? `${clean.slice(0, MAX_TARGET_LENGTH - 1)}…` : clean
}

/**
 * Format a server config / catalog entry into the line the consent card
 * shows. Remote URL wins when both are present (matches how the backend
 * picks the transport); returns null when the source names no target.
 */
export function formatMcpLaunchTarget(source: McpLaunchSource): McpLaunchTarget | null {
  const url = typeof source.url === 'string' ? sanitize(source.url) : ''

  if (url) {
    return { kind: 'remote', target: url }
  }

  const command = typeof source.command === 'string' ? sanitize(source.command) : ''

  if (!command) {
    return null
  }

  const args = (source.args ?? [])
    .map(arg => sanitize(String(arg)))
    .filter(Boolean)
    .join(' ')

  return { kind: 'stdio', target: sanitize(args ? `${command} ${args}` : command) }
}
