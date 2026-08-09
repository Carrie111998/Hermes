import type { FC } from 'react'
import { Fragment, useMemo } from 'react'

import { DirectiveContent, slashSkillRe } from '@/components/assistant-ui/directive-text'
import { referenceRe } from '@/components/assistant-ui/reference-kinds'
import { kanbanCardRefFromMarkdownHref, linkifyKanbanCardRefs } from '@/lib/kanban-card-refs'
import { cn } from '@/lib/utils'

// User messages should render the bare-minimum of markdown: backtick `code`
// spans and ``` fenced blocks. We deliberately don't pull in the full
// assistant Markdown pipeline (Streamdown + KaTeX + syntax highlighter)
// because user input rarely contains structured docs and the heavy pipeline
// adds a lot of runtime cost per bubble.
//
// Directive chips (`@file:`, `@image:`, ...) still resolve via DirectiveContent
// inside the plain-text segments.

interface FenceSegment {
  kind: 'fence'
  code: string
  lang: string | null
}

interface InlineSegment {
  kind: 'inline'
  text: string
}

interface InlineCodeSegment {
  kind: 'inline-code'
  code: string
}

interface InlineTextSegment {
  kind: 'inline-text'
  text: string
}

type TopSegment = FenceSegment | InlineSegment
type InlineNode = InlineCodeSegment | InlineTextSegment

interface DirectiveSegment {
  kind: 'directive'
  text: string
}

interface KanbanTextSegment {
  kind: 'text' | 'kanban-card'
  text: string
  id?: string
  href?: string
}

const FENCE_RE = /```([^\n`]*)\n([\s\S]*?)```/g

// Greedy backtick run length so ``code with `backticks` inside`` works.
const INLINE_CODE_RE = /(`+)([^`\n][\s\S]*?)\1/g

// A directive's value is BACKTICK-QUOTED whenever it needs to be (`@url:`
// always, and any path with a space), so the inline-code scanner would claim
// those backticks first and split one reference into a bare `@url:` plus a code
// span — the composer's chip, flattened on send. Directives win: this is syntax
// the composer wrote, not something the user typed as code.

/** Inline-code matches that don't overlap a directive, so a quoted directive
 *  value reaches DirectiveContent whole. */
function inlineCodeOutsideDirectives(text: string): RegExpMatchArray[] {
  const directives = Array.from(text.matchAll(referenceRe())).map(match => ({
    start: match.index ?? 0,
    end: (match.index ?? 0) + match[0].length
  }))

  return Array.from(text.matchAll(INLINE_CODE_RE)).filter(match => {
    const start = match.index ?? 0
    const end = start + match[0].length

    return !directives.some(directive => start < directive.end && end > directive.start)
  })
}

function splitFences(text: string): TopSegment[] {
  const segments: TopSegment[] = []
  let cursor = 0

  for (const match of text.matchAll(FENCE_RE)) {
    const start = match.index ?? 0

    if (start > cursor) {
      segments.push({ kind: 'inline', text: text.slice(cursor, start) })
    }

    segments.push({
      kind: 'fence',
      lang: (match[1] || '').trim() || null,
      code: match[2] ?? ''
    })
    cursor = start + match[0].length
  }

  if (cursor < text.length) {
    segments.push({ kind: 'inline', text: text.slice(cursor) })
  }

  return segments
}

function splitInlineCode(text: string): InlineNode[] {
  const nodes: InlineNode[] = []
  let cursor = 0

  for (const match of inlineCodeOutsideDirectives(text)) {
    const start = match.index ?? 0

    if (start > cursor) {
      nodes.push({ kind: 'inline-text', text: text.slice(cursor, start) })
    }

    nodes.push({ kind: 'inline-code', code: match[2] })
    cursor = start + match[0].length
  }

  if (cursor < text.length) {
    nodes.push({ kind: 'inline-text', text: text.slice(cursor) })
  }

  return nodes
}

function splitDirectives(text: string): Array<DirectiveSegment | InlineTextSegment> {
  const segments: Array<DirectiveSegment | InlineTextSegment> = []
  let cursor = 0

  const matches = [...Array.from(text.matchAll(referenceRe())), ...Array.from(text.matchAll(slashSkillRe()))].sort(
    (left, right) => (left.index ?? 0) - (right.index ?? 0)
  )

  for (const match of matches) {
    const start = match.index ?? 0

    if (start < cursor) {
      continue
    }

    if (start > cursor) {
      segments.push({ kind: 'inline-text', text: text.slice(cursor, start) })
    }

    segments.push({ kind: 'directive', text: match[0] })
    cursor = start + match[0].length
  }

  if (cursor < text.length) {
    segments.push({ kind: 'inline-text', text: text.slice(cursor) })
  }

  return segments
}

const KANBAN_CARD_MARKDOWN_RE = /\[(@card:[0-9a-f]{4,}|t_[0-9a-f]{4,})\]\((#[^\s)]+)\)/g

function splitKanbanCardRefs(text: string): KanbanTextSegment[] {
  const linked = linkifyKanbanCardRefs(text)
  const segments: KanbanTextSegment[] = []
  let cursor = 0

  for (const match of linked.matchAll(KANBAN_CARD_MARKDOWN_RE)) {
    const start = match.index ?? 0
    const href = match[2] ?? ''
    const id = kanbanCardRefFromMarkdownHref(href)

    if (!id) {
      continue
    }

    if (start > cursor) {
      segments.push({ kind: 'text', text: linked.slice(cursor, start) })
    }

    segments.push({ kind: 'kanban-card', text: match[1] ?? '', href, id })
    cursor = start + match[0].length
  }

  if (cursor < linked.length) {
    segments.push({ kind: 'text', text: linked.slice(cursor) })
  }

  return segments
}

interface UserMessageTextProps {
  text: string
  className?: string
}

export const UserMessageText: FC<UserMessageTextProps> = ({ className, text }) => {
  const top = useMemo(() => splitFences(text), [text])

  return (
    <span className={cn('block', className)} data-slot="aui_user-message-text">
      {top.map((segment, segmentIndex) => {
        if (segment.kind === 'fence') {
          return (
            <pre
              className="my-1.5 max-w-full overflow-x-auto rounded-md border border-(--ui-stroke-tertiary) bg-[color-mix(in_srgb,currentColor_5%,transparent)] px-2.5 py-2 font-mono text-[0.86em] leading-snug"
              data-slot="aui_user-fence"
              key={`fence-${segmentIndex}`}
            >
              <code className="block whitespace-pre">{segment.code}</code>
            </pre>
          )
        }

        return (
          <Fragment key={`inline-${segmentIndex}`}>
            <InlineSegmentView text={segment.text} />
          </Fragment>
        )
      })}
    </span>
  )
}

const InlineSegmentView: FC<{ text: string }> = ({ text }) => {
  const nodes = useMemo(() => splitInlineCode(text), [text])

  return (
    // styles.css bidi hook (#44150); whitespace-pre-line makes each line its own
    // UAX#9 paragraph so it resolves direction independently.
    <span className="wrap-anywhere block whitespace-pre-line" data-slot="aui_user-inline-text">
      {nodes.map((node, nodeIndex) =>
        node.kind === 'inline-code' ? (
          <code
            className="mx-px rounded bg-[color-mix(in_srgb,currentColor_8%,transparent)] px-1 py-px font-mono text-[0.92em]"
            data-slot="aui_user-inline-code"
            key={`code-${nodeIndex}`}
          >
            {node.code}
          </code>
        ) : (
          // Pass plain-text bits through DirectiveContent so @file:/@url: chips
          // still render. DirectiveContent already preserves whitespace.
          <Fragment key={`text-${nodeIndex}`}>
            <InlineTextView text={node.text} />
          </Fragment>
        )
      )}
    </span>
  )
}

const InlineTextView: FC<{ text: string }> = ({ text }) => {
  const segments = useMemo(() => splitDirectives(text), [text])

  return (
    <>
      {segments.map((segment, segmentIndex) =>
        segment.kind === 'directive' ? (
          <DirectiveContent key={`directive-${segmentIndex}`} text={segment.text} />
        ) : (
          <KanbanTextView key={`text-${segmentIndex}`} text={segment.text} />
        )
      )}
    </>
  )
}

const KanbanTextView: FC<{ text: string }> = ({ text }) => {
  const segments = useMemo(() => splitKanbanCardRefs(text), [text])

  return (
    <>
      {segments.map((segment, segmentIndex) =>
        segment.kind === 'kanban-card' ? (
          <a
            className="ref wrap-anywhere"
            data-kanban-card={segment.id}
            href={segment.href}
            key={`kanban-${segmentIndex}`}
          >
            {segment.text}
          </a>
        ) : (
          <Fragment key={`text-${segmentIndex}`}>{segment.text}</Fragment>
        )
      )}
    </>
  )
}
