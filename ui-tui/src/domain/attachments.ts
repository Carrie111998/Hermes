import type { ComposerToken } from '../app/interfaces.js'
import { PASTE_SNIPPET_RE } from '../protocol/paste.js'

/**
 * Composer tokens are the ONE way deferred content shows up in the input line:
 * a collapsed paste and an attached image both render as `[[ … ]]` sitting in
 * the text the user is editing. They are ordinary characters — arrow keys,
 * backspace, and selection work on them for free — and they carry their real
 * payload out-of-band until submit.
 *
 * Two consequences the rest of the composer relies on:
 *   - Deleting the token is how you drop the thing. Nothing else to click.
 *   - Position in the text is meaningful: the model sees the payload where the
 *     token sat, not stapled to the front of the turn.
 */
export const imageToken = (index: number) => `[[ Image ${index} ]]`

/** Highest image token index handed out so far, so a new one never collides. */
export const nextImageIndex = (tokens: ComposerToken[]) =>
  tokens.reduce((max, t) => (t.kind === 'image' ? Math.max(max, t.index) : max), 0) + 1

/** Tokens whose label is no longer anywhere in the composer text. */
export const droppedTokens = (tokens: ComposerToken[], value: string) => {
  const live = new Set(value.match(PASTE_SNIPPET_RE) ?? [])

  return tokens.filter(t => !live.has(t.label))
}

/**
 * A placeholder whose pairing was lost somewhere in the token registration
 * chain. The regex is deliberately broad: it will match e.g. hand-typed
 * `[[ foo ]]`, but the actual paste tokens carry a unique signature —
 * `[N lines]` with a numeric `N`, possibly with a 'k'/'m'/'g' suffix from fmtK.
 * That's how we distinguish between a legit paste token and an arbitrary
 * user-typed string that happens to be bracketed.
 */
export const looksLikeOrphanedPasteToken = (label: string): boolean =>
  PASTE_SNIPPET_RE.test(label) && /\[[\d.]+[kmgt]?\s+lines\]/i.test(label)

export interface ExpandResult {
  expanded: string
  unresolved: string[]
}

/**
 * Resolve every token in `value` to what the agent should actually receive.
 *
 * Repeated identical labels expand in submission order (left to right), which
 * is why this walks matches instead of doing a global replace per token.
 *
 * An image token expands to nothing: the gateway already holds the file in
 * `session.attached_images` and splices the real vision content in at submit.
 * The token's job was to show the user where it landed, so it also eats one
 * adjacent space to avoid leaving a gap in the middle of a sentence.
 *
 * Returns an object containing:
 *   - `expanded`: the fully resolved text
 *   - `unresolved`: an array of placeholder labels that matched the paste-token
 *     regex pattern (`[N lines]`) but had no pairing in the `tokens` array.
 *     These are the orphaned placeholders that would have been sent to the
 *     model verbatim in the original code.
 */
export const expandTokens = (tokens: ComposerToken[]) => {
  const byLabel = new Map<string, ComposerToken[]>()

  for (const token of tokens) {
    const hit = byLabel.get(token.label)
    hit ? hit.push(token) : byLabel.set(token.label, [token])
  }

  return (value: string): ExpandResult => {
    const unresolved: string[] = []

    const expanded = value
      .replace(new RegExp(`[ \\t]?(?:${PASTE_SNIPPET_RE.source})`, 'g'), match => {
        const token = byLabel.get(match.trimStart())?.shift()

        if (!token) {
          if (looksLikeOrphanedPasteToken(match.trimStart())) {
            unresolved.push(match.trimStart())
          }

          return match
        }

        return token.kind === 'paste' ? match.slice(0, match.length - token.label.length) + token.text : ''
      })
      .trim()

    return { expanded, unresolved }
  }
}
