import type { ThreadMessage } from '@assistant-ui/react'
import { describe, expect, it } from 'vitest'

import type { ChatMessage, ChatMessagePart } from '@/lib/chat-messages'
import type { ComposerAttachment } from '@/store/composer'

import {
  attachmentDisplayText,
  attachmentId,
  coalesceToolOnlyAssistants,
  coerceThinkingText,
  createToolMergeCache,
  messageCreatedAt,
  optimisticAttachmentRef,
  parseCommandDispatch,
  parseSlashCommand,
  toRuntimeMessage
} from './chat-runtime'

const DATA_URL = 'data:image/png;base64,iVBORw0KGgoAAAANS'
const THUMB_URL = 'data:image/png;base64,dGh1bWI='

function attachment(overrides: Partial<ComposerAttachment> & Pick<ComposerAttachment, 'kind'>): ComposerAttachment {
  return { id: 'a', label: 'file.png', ...overrides }
}

describe('optimisticAttachmentRef', () => {
  it('renders an image from its in-hand base64 preview (no @image: path ref)', () => {
    const ref = optimisticAttachmentRef(attachment({ kind: 'image', detail: '/tmp/shot.png', previewUrl: DATA_URL }))

    // The raw data URL flows through extractEmbeddedImages → inline thumbnail,
    // dodging the remote /api/media 403 an @image:<localpath> ref would hit.
    expect(ref).toBe(DATA_URL)
  })

  it('prefers the downscaled thumbnail for the display ref when present', () => {
    const ref = optimisticAttachmentRef(
      attachment({ kind: 'image', detail: '/tmp/shot.png', previewUrl: DATA_URL, thumbnailUrl: THUMB_URL })
    )

    // The bubble is display-only; full bytes are read on demand and for upload.
    expect(ref).toBe(THUMB_URL)
  })

  it('does not render a full path-backed image while its bounded thumbnail is pending', () => {
    expect(optimisticAttachmentRef(attachment({ kind: 'image', detail: '/tmp/shot.png' }))).toBeNull()
  })

  it('does not use a path fallback for a non-data preview url', () => {
    const ref = optimisticAttachmentRef(
      attachment({ kind: 'image', detail: '/tmp/shot.png', previewUrl: 'https://example.com/x.png' })
    )

    expect(ref).toBeNull()
  })

  it('passes non-image attachments straight through to attachmentDisplayText', () => {
    expect(optimisticAttachmentRef(attachment({ kind: 'file', refText: '@file:src/a.ts', previewUrl: DATA_URL }))).toBe(
      '@file:src/a.ts'
    )
  })

  // Session switches / draft restores can leave undefined|null holes in the
  // composer attachments array. AttachmentList already filters them (#49624),
  // but the submit path maps the same array through these helpers — an unguarded
  // hole threw "Cannot read properties of undefined (reading 'refText')",
  // crashing the chat surface (blank pane). The helpers must no-op on holes.
  it('returns null for an undefined attachment instead of throwing', () => {
    expect(() => optimisticAttachmentRef(undefined as unknown as ComposerAttachment)).not.toThrow()
    expect(optimisticAttachmentRef(undefined as unknown as ComposerAttachment)).toBeNull()
  })

  it('returns null for a null attachment instead of throwing', () => {
    expect(optimisticAttachmentRef(null as unknown as ComposerAttachment)).toBeNull()
  })
})

describe('attachmentDisplayText', () => {
  it('returns null for undefined|null instead of reading .kind/.refText on a hole', () => {
    expect(() => attachmentDisplayText(undefined as unknown as ComposerAttachment)).not.toThrow()
    expect(attachmentDisplayText(undefined as unknown as ComposerAttachment)).toBeNull()
    expect(attachmentDisplayText(null as unknown as ComposerAttachment)).toBeNull()
  })

  it('still resolves a normal file ref', () => {
    expect(attachmentDisplayText(attachment({ kind: 'file', refText: '@file:src/a.ts' }))).toBe('@file:src/a.ts')
  })

  it('expands a review attachment into an anchored fenced block', () => {
    const detail = JSON.stringify({
      author: 'teknium1',
      body: 'this cap looks wrong',
      diffHunk: '@@ -1,2 +1,2 @@\n-const CAP = 5\n+const CAP = 50',
      kind: 'review',
      line: 12,
      path: 'src/limits.ts',
      prNumber: 123,
      startLine: null,
      url: 'https://github.com/o/r/pull/123#discussion_r1'
    })

    const block = attachmentDisplayText(attachment({ kind: 'review', detail, refText: '@url:`https://x`' }))

    // The contract: anchor (file:line), author, body, and the hunk all ride.
    expect(block).toContain('review-comment src/limits.ts:12')
    expect(block).toContain('@teknium1')
    expect(block).toContain('this cap looks wrong')
    expect(block).toContain('const CAP = 50')
  })

  it('falls back to the url ref when a review detail is malformed', () => {
    expect(attachmentDisplayText(attachment({ kind: 'review', detail: 'not json', refText: '@url:`https://x`' }))).toBe(
      '@url:`https://x`'
    )
  })
})

describe('coerceThinkingText', () => {
  it('strips streaming status prefixes from thinking deltas', () => {
    expect(coerceThinkingText("◉_◉ processing... checking the user's request")).toBe("checking the user's request")
    expect(coerceThinkingText('(¬‿¬) analyzing... reading the file')).toBe('reading the file')
  })

  it('drops empty thinking rewrite placeholder text', () => {
    expect(
      coerceThinkingText(
        "◉_◉ processing... I don't see any current rewritten thinking or next thinking to process. Could you provide the thinking content you'd like me to rewrite?"
      )
    ).toBe('')
  })
})

describe('parseCommandDispatch', () => {
  it('keeps the notice on a send directive (e.g. /goal set)', () => {
    // The backend's /goal set returns {type:send, notice:"⊙ Goal set …", message}.
    // Dropping the notice made /goal look like it did nothing in the desktop app.
    const parsed = parseCommandDispatch({ type: 'send', notice: '⊙ Goal set', message: 'do the thing' })

    expect(parsed).toEqual({ type: 'send', message: 'do the thing', notice: '⊙ Goal set' })
  })

  it('keeps message-only send directives working (no notice)', () => {
    expect(parseCommandDispatch({ type: 'send', message: 'hi' })).toEqual({
      type: 'send',
      message: 'hi',
      notice: undefined
    })
  })

  it('parses a prefill directive with its notice (e.g. /undo)', () => {
    const parsed = parseCommandDispatch({ type: 'prefill', notice: 'backed up 1 turn', message: 'edit me' })

    expect(parsed).toEqual({ type: 'prefill', message: 'edit me', notice: 'backed up 1 turn' })
  })

  it('rejects a prefill directive missing its message', () => {
    expect(parseCommandDispatch({ type: 'prefill', notice: 'x' })).toBeNull()
  })
})

describe('parseSlashCommand', () => {
  it('parses a single-line command', () => {
    expect(parseSlashCommand('/some-skill do something')).toEqual({
      arg: 'do something',
      name: 'some-skill'
    })
  })

  it('keeps a multiline arg intact instead of failing the whole parse (#41323)', () => {
    expect(parseSlashCommand('/goal Write a Python script\nthat prints Hello World')).toEqual({
      arg: 'Write a Python script\nthat prints Hello World',
      name: 'goal'
    })
  })

  it('parses a skill command with a long pasted multi-paragraph context (#55510)', () => {
    const context = 'summarize this:\n\nparagraph one\nparagraph two\n\nparagraph three'

    expect(parseSlashCommand(`/some-skill ${context}`)).toEqual({
      arg: context,
      name: 'some-skill'
    })
  })

  it('takes the name across a newline boundary like the CLI and gateway (split on any whitespace)', () => {
    expect(parseSlashCommand('/goal\npasted block')).toEqual({ arg: 'pasted block', name: 'goal' })
  })

  it('keeps truly empty slash input empty', () => {
    expect(parseSlashCommand('/')).toEqual({ arg: '', name: '' })
    expect(parseSlashCommand('/   ')).toEqual({ arg: '', name: '' })
  })

  it('does not treat text after horizontal whitespace as a command name (CLI parity)', () => {
    expect(parseSlashCommand('/ some words')).toEqual({ arg: '', name: '' })
  })
})

describe('attachmentId', () => {
  it('normalizes a trailing slash on a url so a re-attach dedupes (#59305 P2)', () => {
    expect(attachmentId('url', 'https://example.com/a')).toBe(attachmentId('url', 'https://example.com/a/'))
  })

  it('falls back to the trimmed raw value for a malformed url instead of throwing', () => {
    expect(() => attachmentId('url', 'not a url')).not.toThrow()
    expect(attachmentId('url', '  not a url  ')).toBe(attachmentId('url', 'not a url'))
  })

  it('normalizes backslash path separators so a Windows and posix path dedupe', () => {
    expect(attachmentId('file', 'a\\b.ts')).toBe(attachmentId('file', 'a/b.ts'))
  })

  it('normalizes a trailing slash on a folder path', () => {
    expect(attachmentId('folder', 'src/app/')).toBe(attachmentId('folder', 'src/app'))
  })

  it('does not collapse a bare root path to an empty id', () => {
    expect(attachmentId('folder', '/')).toBe('folder:/')
  })

  it('keeps distinct urls distinct', () => {
    expect(attachmentId('url', 'https://example.com/a')).not.toBe(attachmentId('url', 'https://example.com/b'))
  })
})

describe('messageCreatedAt', () => {
  const NOW = Date.UTC(2026, 6, 28, 18, 0, 0)

  it('reads the authoritative Unix-seconds timestamp (not ms)', () => {
    // 1785282262s → July 2026, not the 1970 epoch a *1000-less read would give.
    expect(messageCreatedAt({ timestamp: 1785282262 }, NOW).getFullYear()).toBe(2026)
  })

  it('falls back to now — never digs digits out of the id → "20663d ago" (1970)', () => {
    // The old fallback did `new Date(Number(id.match(/\d+/)))`, so a session-style
    // id like 20260728_184420_05e697 parsed to 20260728 *ms* = Jan 1970, showing
    // as an absurd 20663-day age. A timestamp-less message is freshly created.
    expect(messageCreatedAt({ timestamp: undefined }, NOW).getTime()).toBe(NOW)
  })

  it('treats a zero / non-finite timestamp as absent', () => {
    expect(messageCreatedAt({ timestamp: 0 }, NOW).getTime()).toBe(NOW)
    expect(messageCreatedAt({ timestamp: Number.NaN }, NOW).getTime()).toBe(NOW)
  })
})

describe('toRuntimeMessage timeline metadata', () => {
  it('does not expose a fabricated visible timestamp for timestamp-less history', () => {
    const runtime = toRuntimeMessage({
      id: 'old-message',
      parts: [{ text: 'old', type: 'text' }],
      role: 'assistant'
    })

    expect((runtime.metadata?.custom as { timelineTimestamp?: number }).timelineTimestamp).toBeUndefined()
  })
})

describe('coalesceToolOnlyAssistants toolCallId uniqueness', () => {
  // Regression contract for #87857: two individually-clean assistant rows can
  // share a toolCallId (structural carry-over re-attaching a cached row's tool
  // calls while the same turn also exists as a committed row). Folding them
  // used to manufacture ONE message carrying the id twice — the exact shape
  // that makes assistant-ui's useResources throw and crash-loop the pane.
  const tool = (toolCallId: string): ChatMessagePart =>
    ({ type: 'tool-call', toolCallId, toolName: 'terminal', args: {} as never, argsText: '' }) as ChatMessagePart

  const assistant = (id: string, parts: ChatMessagePart[]): ChatMessage =>
    ({ id, role: 'assistant', parts }) as unknown as ChatMessage

  it('drops the copy the predecessor already carries, keeps the new call', () => {
    const merged = coalesceToolOnlyAssistants(
      [
        assistant('committed-49-assistant', [
          { type: 'text', text: 'working' } as ChatMessagePart,
          tool('call-a'),
          tool('call-b')
        ]),
        assistant('assistant-stream-49', [tool('call-b'), tool('call-c')])
      ],
      createToolMergeCache()
    )

    expect(merged).toHaveLength(1)

    const ids = merged[0].parts
      .filter(part => part.type === 'tool-call')
      .map(part => (part as { toolCallId: string }).toolCallId)

    expect(ids).toEqual(['call-a', 'call-b', 'call-c'])
  })

  it('folds a clean follow-up unchanged', () => {
    const merged = coalesceToolOnlyAssistants(
      [
        assistant('a1', [{ type: 'text', text: 'ok' } as ChatMessagePart, tool('call-a')]),
        assistant('a2', [tool('call-b')])
      ],
      createToolMergeCache()
    )

    expect(merged).toHaveLength(1)

    const ids = merged[0].parts
      .filter(part => part.type === 'tool-call')
      .map(part => (part as { toolCallId: string }).toolCallId)

    expect(ids).toEqual(['call-a', 'call-b'])
  })
})

describe('toRuntimeMessage tool-call id uniqueness', () => {
  // Regression contract for "Duplicate key toolCallId-terminal_0 in
  // useResources": Kimi's anthropic_messages mode numbers tool_use blocks per
  // API response, so `terminal_0` recurs across the whole session, and any
  // merge/graft/backfill path can land two of them in ONE rendered message.
  // assistant-ui keys parts by toolCallId and throws on a duplicate, taking
  // the workspace pane down. toRuntimeMessage is the last boundary before the
  // runtime, so uniqueness is enforced there, per message.
  const toolCall = (toolCallId: string): ChatMessagePart => ({
    type: 'tool-call',
    toolCallId,
    toolName: 'terminal',
    args: {} as never,
    argsText: ''
  })

  const renderedToolCallIds = (runtime: ThreadMessage): string[] =>
    (runtime.content as ChatMessagePart[])
      .filter(part => part.type === 'tool-call')
      .map(part => (part as { toolCallId: string }).toolCallId)

  it('never hands the runtime two parts with the same toolCallId', () => {
    const runtime = toRuntimeMessage({
      id: 'a1',
      role: 'assistant',
      parts: [toolCall('terminal_0'), { text: 'done', type: 'text' }, toolCall('terminal_0'), toolCall('terminal_0')]
    })

    const ids = renderedToolCallIds(runtime)

    expect(new Set(ids).size).toBe(ids.length)
    // The first occurrence keeps the provider id, so keys stay stable across
    // re-renders for the overwhelmingly common unique case.
    expect(ids[0]).toBe('terminal_0')
  })

  it('covers the tool-only coalesce path: two merged bubbles that share an id still render unique', () => {
    const merged = coalesceToolOnlyAssistants(
      [
        { id: 'a1', role: 'assistant', parts: [toolCall('terminal_0')] },
        { id: 'a2', role: 'assistant', parts: [toolCall('terminal_0'), toolCall('read_file_0')] }
      ],
      createToolMergeCache()
    )

    expect(merged).toHaveLength(1)

    const ids = renderedToolCallIds(toRuntimeMessage(merged[0]))

    // This asserted 3 parts when it was written, because coalescing carried
    // BOTH copies of terminal_0 through and toRuntimeMessage renamed the
    // second. Upstream #87857 then taught coalesceToolOnlyAssistants to drop
    // the copy the predecessor already carries, so only 2 parts reach the
    // boundary. The end-to-end contract is unchanged and is what this asserts:
    // whatever coalescing hands over, the runtime never sees a repeated id,
    // and no distinct tool call is lost on the way.
    expect(new Set(ids).size).toBe(ids.length)
    expect(ids).toEqual(['terminal_0', 'read_file_0'])
  })

  it('preserves parts-array reference identity when every id is already unique', () => {
    const parts: ChatMessagePart[] = [toolCall('terminal_0'), toolCall('terminal_1')]
    const runtime = toRuntimeMessage({ id: 'a1', role: 'assistant', parts })

    expect(runtime.content).toBe(parts)
  })
})
