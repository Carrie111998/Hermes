import { SLASH_COMMAND_RE } from './chat-runtime'

/** Matches `agent/title_generator.py`'s MAX_DERIVED_TITLE_CHARS, so a draft
 *  doesn't visibly reflow the moment the backend's derived title replaces it. */
const MAX_DRAFT_TITLE_CHARS = 48

/**
 * Name a draft after what the user has typed into it.
 *
 * The client-side twin of the backend's `derive_title`: first meaningful line,
 * whitespace collapsed, cut on a word boundary. It runs before any session
 * exists, so it can't reach the real titler — a draft has no persisted row and
 * no opening message yet, which is exactly what `apply_instant_title` needs.
 *
 * Empty when there's nothing worth naming, so the caller keeps "New session"
 * rather than showing a title that says less than the placeholder.
 */
export function deriveDraftTitle(text: string): string {
  const line =
    text
      .split('\n')
      .find(candidate => candidate.trim())
      ?.trim() ?? ''

  if (!line) {
    return ''
  }

  // A bare `/skin` names the draft after the command rather than the work, the
  // failure the backend titler summarizes away. Title from the argument instead;
  // with no argument there is no intent yet, so the placeholder stands.
  const body = (SLASH_COMMAND_RE.test(line) ? line.replace(/^\/\S+\s*/, '') : line).split(/\s+/).join(' ')

  if (body.length <= MAX_DRAFT_TITLE_CHARS) {
    return body
  }

  // Cut on a word boundary, unless that would throw away more than half of it.
  const cut = body.slice(0, MAX_DRAFT_TITLE_CHARS)
  const space = cut.lastIndexOf(' ')
  const kept = space > MAX_DRAFT_TITLE_CHARS / 2 ? cut.slice(0, space) : cut

  return `${kept.replace(/[\s,.;:—-]+$/, '')}…`
}

/** When a tab may take its name from unsent composer text.
 *
 *  Draft titles exist only for a true new chat (no stored id yet) or an unused
 *  + / ⌘T tab that has never listed a row and has no transcript. The moment a
 *  session is listed, has a title, or already has messages — including a
 *  compression ancestor whose recents row vanished — overlaying `deriveDraftTitle`
 *  makes every open tab look like "New session" / the typed fragment. */
export function shouldUseDraftTabTitle(opts: {
  storedSessionId: string | null
  listedRow?: { message_count?: null | number; title?: null | string } | null
  hasMessages?: boolean
}): boolean {
  const id = opts.storedSessionId?.trim() ?? ''

  if (!id) {
    return true
  }

  const row = opts.listedRow

  if (row && ((row.message_count ?? 0) > 0 || Boolean(row.title?.trim()))) {
    return false
  }

  if (opts.hasMessages) {
    return false
  }

  return true
}
