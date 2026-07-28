import { atom } from 'nanostores'

// MCP Apps card→model channels, per the io.modelcontextprotocol/ui spec.
//
// `ui/update-model-context` — SILENT state snapshot for the model. Spec: "Each
// request overwrites the previous context sent by the View"; the host SHOULD
// only hand the LAST update per view to the model, and MAY defer delivery
// until the next user message (including `ui/message`). We stage snapshots in
// a per-view slot here and flush them as a prefix of the next outgoing user
// message (`buildOutgoingUserText`).
//
// `ui/message` — a conversation message with a role that DOES trigger a
// follow-up turn. McpAppCard publishes it to `$mcpAppUserMessage`; the
// controller routes it through the normal `submitText` send path.

export interface McpAppUserMessage {
  /** Monotonic id so the same text can be requested twice in a row. */
  id: number
  text: string
}

export const $mcpAppUserMessage = atom<McpAppUserMessage | null>(null)

let seq = 0

export function requestMcpAppUserMessage(text: string): void {
  const trimmed = text.trim()

  if (!trimmed) {
    return
  }

  seq += 1
  $mcpAppUserMessage.set({ id: seq, text: trimmed })
}

export function clearMcpAppUserMessage(): void {
  $mcpAppUserMessage.set(null)
}

// Staged model context: one slot per view (card instance), overwrite on update.
// Insertion order is kept so multi-card context reads in card order.
const stagedModelContext = new Map<string, string>()

/** Overwrite the staged model-context snapshot for one view (spec semantics). */
export function stageModelContext(viewId: string, text: string): void {
  const trimmed = text.trim()

  if (!trimmed) {
    // An empty update clears this view's snapshot.
    stagedModelContext.delete(viewId)

    return
  }

  stagedModelContext.set(viewId, trimmed)
}

/** Drain all staged snapshots (latest per view), in staging order. */
export function consumeStagedModelContext(): string {
  if (!stagedModelContext.size) {
    return ''
  }

  const text = [...stagedModelContext.values()].join('\n\n')

  stagedModelContext.clear()

  return text
}

/**
 * Compose the text actually sent to the model for a user message: any staged
 * card context (delivered with the next user message, per the spec's deferred
 * delivery allowance) followed by the user's own text.
 */
export function buildOutgoingUserText(userText: string): string {
  const staged = consumeStagedModelContext()

  return staged ? `${staged}\n\n${userText}` : userText
}
