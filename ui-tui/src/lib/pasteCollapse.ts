/**
 * Paste-collapse decision shared by the bracketed-paste handler.
 *
 * A paste collapses to a `[[ Paste N ]]` file reference when it is too big to
 * sit in the composer raw:
 *
 * - `linesThreshold` — enough newlines (multi-line dumps, pasted code blocks).
 * - `charsThreshold` — enough total chars on any shape of line (the
 *   "8000 chars of minified JSON on one line" guard, #32447).
 * - `dataCharsThreshold` — a whitespace-free single line long enough that it
 *   can only be data (unit/ID streams, base64, log lines, minified output).
 *   Prose always contains whitespace, so this never fires on normal text, but
 *   it catches the ~1-2KB data dumps that sit below `charsThreshold` and
 *   would otherwise flood the input box raw.
 *
 * `*Threshold` values of 0 disable that particular guard (mirrors the
 * `paste_collapse_*` config semantics).
 */
export const DATA_LIKE_PASTE_CHARS = 500

export interface PasteCollapseInput {
  text: string
  lineCount: number
  linesThreshold: number
  charsThreshold: number
  dataCharsThreshold?: number
}

export function shouldCollapsePaste({
  text,
  lineCount,
  linesThreshold,
  charsThreshold,
  dataCharsThreshold = DATA_LIKE_PASTE_CHARS
}: PasteCollapseInput): boolean {
  if (linesThreshold > 0 && lineCount >= linesThreshold) {
    return true
  }

  if (charsThreshold > 0 && text.length >= charsThreshold) {
    return true
  }

  return (
    dataCharsThreshold > 0 &&
    text.length >= dataCharsThreshold &&
    !/\s/.test(text)
  )
}
