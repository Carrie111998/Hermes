import { describe, expect, it } from 'vitest'

import { notesChangedForUpdate, parseReleaseNotes } from './release-notes'

const SAMPLE = `# Hermes v0.21.0 (2026.8.22)

## What's new
- Messages queued while Hermes was busy no longer get lost
- A new setup page checks for missing prerequisites

## Fixed
- Sidebar no longer jitters while dragging

## Improved
- Clearer wording on update prompts
`

describe('parseReleaseNotes', () => {
  it('parses headings and bullets into sections with mapped group ids', () => {
    const sections = parseReleaseNotes(SAMPLE)

    expect(sections).toEqual([
      {
        id: 'new',
        label: "What's new",
        items: ['Messages queued while Hermes was busy no longer get lost', 'A new setup page checks for missing prerequisites']
      },
      { id: 'fixed', label: 'Fixed', items: ['Sidebar no longer jitters while dragging'] },
      { id: 'improved', label: 'Improved', items: ['Clearer wording on update prompts'] }
    ])
  })

  it('treats unknown section headings as the other group', () => {
    const sections = parseReleaseNotes('# v1\n\n## Experiments\n- Weird stuff\n')

    expect(sections).toEqual([{ id: 'other', label: 'Experiments', items: ['Weird stuff'] }])
  })

  it('drops sections that have no items', () => {
    const sections = parseReleaseNotes('# v1\n\n## Fixed\n\n## Faster\n- Snappier startup\n')

    expect(sections).toEqual([{ id: 'faster', label: 'Faster', items: ['Snappier startup'] }])
  })

  it('ignores bullets outside any section', () => {
    const sections = parseReleaseNotes('- orphan bullet\n\n## Fixed\n- Real item\n')

    expect(sections).toEqual([{ id: 'fixed', label: 'Fixed', items: ['Real item'] }])
  })

  it('returns null when the file has no usable items', () => {
    expect(parseReleaseNotes('')).toBeNull()
    expect(parseReleaseNotes('# v1\n\n## Fixed\n\n')).toBeNull()
    expect(parseReleaseNotes('just prose, no sections')).toBeNull()
  })

  it('tolerates CRLF line endings and trailing spaces', () => {
    const sections = parseReleaseNotes('# v1\r\n\r\n## Fixed  \r\n- Item one  \r\n- Item two\r\n')

    expect(sections).toEqual([{ id: 'fixed', label: 'Fixed', items: ['Item one', 'Item two'] }])
  })
})

describe('notesChangedForUpdate', () => {
  it('is true when the remote notes blob differs from the local one', () => {
    expect(notesChangedForUpdate('aaa', 'bbb')).toBe(true)
  })

  it('is true when the local checkout predates the notes file entirely', () => {
    expect(notesChangedForUpdate(null, 'bbb')).toBe(true)
  })

  it('is false when the remote has the same notes the user already saw', () => {
    expect(notesChangedForUpdate('aaa', 'aaa')).toBe(false)
  })

  it('is false when the remote branch carries no notes file', () => {
    expect(notesChangedForUpdate('aaa', null)).toBe(false)
    expect(notesChangedForUpdate(null, null)).toBe(false)
  })
})
