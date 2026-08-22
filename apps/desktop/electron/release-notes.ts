/**
 * Parse the committed RELEASE_NOTES.md into structured sections for the
 * update overlay. The file is written at release time (scripts/release.py)
 * as plain-English notes; parsing it beats showing raw commit subjects.
 *
 * Pure and dependency-free so it can be unit-tested without Electron.
 */

export interface ReleaseNotesSection {
  id: string
  label: string
  items: string[]
}

/** Section heading → renderer group id. Unknown headings become 'other'. */
const LABEL_TO_ID: Record<string, string> = {
  "What's new": 'new',
  Fixed: 'fixed',
  Faster: 'faster',
  Improved: 'improved',
  'Other improvements': 'other'
}

/**
 * Parse RELEASE_NOTES.md markdown:
 *   # Hermes v1.2.3 (2026.8.19)      ← ignored (title line)
 *   ## What's new                    ← section heading
 *   - item one                       ← bullet, attached to current section
 *   - item two
 *
 * Returns null when the file has no usable sections or items, so callers
 * fall back to parsing raw commit subjects.
 */
export function parseReleaseNotes(markdown: string): ReleaseNotesSection[] | null {
  const sections: ReleaseNotesSection[] = []
  let current: ReleaseNotesSection | null = null

  for (const rawLine of markdown.split(/\r?\n/)) {
    const line = rawLine.trimEnd()

    if (line.startsWith('## ')) {
      const label = line.slice(3).trim()

      if (!label) {
        continue
      }

      current = { id: LABEL_TO_ID[label] ?? 'other', label, items: [] }
      sections.push(current)

      continue
    }

    if (line.startsWith('- ')) {
      const item = line.slice(2).trim()

      if (current && item) {
        current.items.push(item)
      }
    }
  }

  const nonEmpty = sections.filter(section => section.items.length > 0)

  return nonEmpty.length > 0 ? nonEmpty : null
}

/**
 * The overlay updates on every new commit, but RELEASE_NOTES.md only changes
 * when a release lands. Showing notes the user already saw for the release
 * they are on would bury the commits they are actually about to get, so the
 * notes are only worth rendering when the remote blob differs from the local
 * one. Both args are blob shas from `git rev-parse <rev>:RELEASE_NOTES.md`;
 * either may be null when the file does not exist at that rev.
 */
export function notesChangedForUpdate(localNotesSha: string | null, remoteNotesSha: string | null): boolean {
  return remoteNotesSha != null && remoteNotesSha !== localNotesSha
}
