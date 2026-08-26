import { describe, expect, it } from 'vitest'

import {
  advanceDraftVersion,
  createSlashClearTracker,
  isAttachmentScopeCurrent,
  looksLikeDroppedPath,
  resolveSlashAttachment
} from '../app/useComposerState.js'

describe('advanceDraftVersion', () => {
  it('keeps attachment-command clears in the active draft', () => {
    expect(advanceDraftVersion(4, true)).toBe(4)
  })

  it('advances ordinary submit and cancel clears', () => {
    expect(advanceDraftVersion(4, false)).toBe(5)
  })
})

describe('createSlashClearTracker', () => {
  it('preserves the draft when a synchronous canonical attachment marks the pending clear', () => {
    const tracker = createSlashClearTracker()
    const scope = tracker.begin(4, 'sid-a')

    tracker.markAttachment(scope)

    expect(tracker.clear(4)).toBe(4)
    expect(scope).toEqual({ draftVersion: 4, sid: 'sid-a' })
  })

  it('does not leak preservation from an async alias into the next clear', () => {
    const tracker = createSlashClearTracker()
    const scope = tracker.begin(4, 'sid-a')

    expect(tracker.clear(4)).toBe(5)
    expect(scope).toEqual({ draftVersion: 5, sid: 'sid-a' })

    // command.dispatch may resolve an alias to /image after the originating
    // slash was already cleared. That late action has no paired clear to mark.
    tracker.markAttachment(scope)

    expect(tracker.clear(5)).toBe(6)
  })
})

describe('isAttachmentScopeCurrent', () => {
  const scope = { draftVersion: 5, sid: 'sid-a' }

  it('accepts a carried alias scope only in its originating draft and session', () => {
    expect(isAttachmentScopeCurrent(scope, 5, 'sid-a')).toBe(true)
    expect(isAttachmentScopeCurrent(scope, 6, 'sid-a')).toBe(false)
    expect(isAttachmentScopeCurrent(scope, 5, 'sid-b')).toBe(false)
  })

  it('accepts direct attachment actions without a slash scope', () => {
    expect(isAttachmentScopeCurrent(undefined, 5, 'sid-a')).toBe(true)
  })
})

describe('looksLikeDroppedPath', () => {
  it('recognizes macOS screenshot temp paths and file URIs', () => {
    expect(looksLikeDroppedPath('/var/folders/x/T/TemporaryItems/Screenshot\\ 2026-04-21\\ at\\ 1.04.43 PM.png')).toBe(
      true
    )
    expect(
      looksLikeDroppedPath('file:///var/folders/x/T/TemporaryItems/Screenshot%202026-04-21%20at%201.04.43%20PM.png')
    ).toBe(true)
  })

  it('rejects normal multiline or plain text paste', () => {
    expect(looksLikeDroppedPath('hello world')).toBe(false)
    expect(looksLikeDroppedPath('line one\nline two')).toBe(false)
  })

  it('recognizes common image file extensions', () => {
    expect(looksLikeDroppedPath('/Users/me/Desktop/photo.jpg')).toBe(true)
    expect(looksLikeDroppedPath('/Users/me/Desktop/diagram.png')).toBe(true)
    expect(looksLikeDroppedPath('/tmp/capture.webp')).toBe(true)
    expect(looksLikeDroppedPath('/tmp/image.gif')).toBe(true)
  })

  it('recognizes file:// URIs with various extensions', () => {
    expect(looksLikeDroppedPath('file:///home/user/doc.pdf')).toBe(true)
    expect(looksLikeDroppedPath('file:///tmp/screenshot.png')).toBe(true)
  })

  it('recognizes paths with spaces (not backslash-escaped)', () => {
    expect(looksLikeDroppedPath('/var/folders/x/T/TemporaryItems/Screenshot 2026-04-21 at 1.04.43 PM.png')).toBe(true)
  })

  it('rejects empty/whitespace-only input', () => {
    expect(looksLikeDroppedPath('')).toBe(false)
    expect(looksLikeDroppedPath('   ')).toBe(false)
    expect(looksLikeDroppedPath('\n')).toBe(false)
  })

  it('rejects URLs that are not file:// URIs', () => {
    expect(looksLikeDroppedPath('https://example.com/image.png')).toBe(false)
    expect(looksLikeDroppedPath('http://localhost/file.pdf')).toBe(false)
  })

  it('rejects short slash-like strings without path structure', () => {
    // No second '/' or '.' → not a plausible file path
    expect(looksLikeDroppedPath('/help')).toBe(false)
    expect(looksLikeDroppedPath('/model sonnet')).toBe(false)
    expect(looksLikeDroppedPath('/api')).toBe(false)
  })

  it('accepts absolute paths with directory separators or extensions', () => {
    expect(looksLikeDroppedPath('/usr/bin/test')).toBe(true)
    expect(looksLikeDroppedPath('/tmp/file.txt')).toBe(true)
    expect(looksLikeDroppedPath('/etc/hosts')).toBe(true) // has second /
  })
})

describe('resolveSlashAttachment', () => {
  it('does not resurrect the consumed slash command after the RPC resolves', async () => {
    let resolveRequest!: (value: { name: string; path: string }) => void
    let composer = { draftVersion: 0, sid: 'sid-a', value: '/image /tmp/dashboard.png' }
    let discarded = false

    const request = new Promise<{ name: string; path: string }>(resolve => {
      resolveRequest = resolve
    })

    const pending = resolveSlashAttachment(
      () => request,
      () => composer,
      0,
      'sid-a',
      (_attached, value, cursor) => ({
        cursor: cursor + 13,
        value: `${value}[[ Image 1 ]]`
      }),
      () => {
        discarded = true
      },
      value => {
        composer.value = value
      }
    )

    composer = { draftVersion: 0, sid: 'sid-a', value: '' }
    resolveRequest({ name: 'dashboard.png', path: '/tmp/dashboard.png' })

    await expect(pending).resolves.toMatchObject({ value: '[[ Image 1 ]]' })
    expect(composer.value).toBe('[[ Image 1 ]]')
    expect(discarded).toBe(false)
  })

  it('preserves prompt text typed while the attachment is in flight', async () => {
    let composer = { draftVersion: 0, sid: 'sid-a', value: '/image /tmp/dashboard.png' }
    let resolveRequest!: (value: { name: string; path: string }) => void

    const request = new Promise<{ name: string; path: string }>(resolve => {
      resolveRequest = resolve
    })

    const pending = resolveSlashAttachment(
      () => request,
      () => composer,
      0,
      'sid-a',
      (_attached, value, cursor) => ({ cursor: cursor + 13, value: `${value} [[ Image 1 ]]` }),
      () => {
        throw new Error('current attachment should not be discarded')
      },
      value => {
        composer.value = value
      }
    )

    composer = { draftVersion: 0, sid: 'sid-a', value: 'explain this' }
    resolveRequest({ name: 'dashboard.png', path: '/tmp/dashboard.png' })

    await expect(pending).resolves.toMatchObject({ value: 'explain this [[ Image 1 ]]' })
    expect(composer.value).toBe('explain this [[ Image 1 ]]')
  })

  it('discards an attachment after a later submission clears the composer again', async () => {
    const attached = { name: 'dashboard.png', path: '/tmp/dashboard.png' }
    let applied = false
    let discarded: typeof attached | undefined

    const result = await resolveSlashAttachment(
      async () => attached,
      () => ({ draftVersion: 1, sid: 'sid-a', value: 'next draft' }),
      0,
      'sid-a',
      () => {
        applied = true

        return { cursor: 0, value: 'wrong' }
      },
      value => {
        discarded = value
      },
      () => {
        throw new Error('discarded attachment should not update the composer')
      }
    )

    expect(result).toBeNull()
    expect(applied).toBe(false)
    expect(discarded).toEqual(attached)
  })

  it('keeps earlier attachments when a batch issues another image slash command', async () => {
    const attached = { name: 'second.png', path: '/tmp/second.png' }
    const composer = { draftVersion: 0, sid: 'sid-a', value: '[[ Image 1 ]]' }

    const result = await resolveSlashAttachment(
      async () => attached,
      () => composer,
      0,
      'sid-a',
      (_attachment, value) => ({ cursor: value.length + 14, value: `${value} [[ Image 2 ]]` }),
      () => {
        throw new Error('same-draft attachment should not be discarded')
      },
      value => {
        composer.value = value
      }
    )

    expect(result).toEqual({ cursor: 27, value: '[[ Image 1 ]] [[ Image 2 ]]' })
    expect(composer.value).toBe('[[ Image 1 ]] [[ Image 2 ]]')
  })

  it('discards an attachment after its originating session changes', async () => {
    const attached = { name: 'dashboard.png', path: '/tmp/dashboard.png' }
    let discarded: typeof attached | undefined

    const result = await resolveSlashAttachment(
      async () => attached,
      () => ({ draftVersion: 0, sid: 'sid-b', value: '' }),
      0,
      'sid-a',
      () => {
        throw new Error('stale-session attachment should not update the composer')
      },
      value => {
        discarded = value
      },
      () => {
        throw new Error('stale-session attachment should not update the composer')
      }
    )

    expect(result).toBeNull()
    expect(discarded).toEqual(attached)
  })

  it('commits concurrent attachment resolutions without hiding either token', async () => {
    const composer = { draftVersion: 0, sid: 'sid-a', value: '' }
    const tokens: string[] = []

    const attach = (_attachment: { name: string; path: string }, value: string) => {
      const token = `[[ Image ${tokens.length + 1} ]]`

      tokens.push(token)

      return { cursor: value.length + token.length, value: `${value}${token}` }
    }

    const apply = (value: string) => {
      composer.value = value
    }

    const discard = () => {
      throw new Error('same-draft attachment should not be discarded')
    }

    await Promise.all([
      resolveSlashAttachment(
        async () => ({ name: 'first.png', path: '/tmp/first.png' }),
        () => composer,
        0,
        'sid-a',
        attach,
        discard,
        apply
      ),
      resolveSlashAttachment(
        async () => ({ name: 'second.png', path: '/tmp/second.png' }),
        () => composer,
        0,
        'sid-a',
        attach,
        discard,
        apply
      )
    ])

    expect(composer.value).toBe('[[ Image 1 ]][[ Image 2 ]]')
    expect(tokens).toEqual(['[[ Image 1 ]]', '[[ Image 2 ]]'])
  })
})
