import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { artifactsForSession, clearArtifactRegistry } from '@/store/artifacts'
import { $previewTabs } from '@/store/preview'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'

import { MarkdownTextContent } from './markdown-text'

const HTML_DOC = `<!doctype html>
<html>
<head><title>Pomodoro Timer</title></head>
<body>
<h1>Pomodoro</h1>
<p>A tiny focus timer that counts down twenty-five minutes.</p>
<script>let seconds = 25 * 60; setInterval(() => { seconds -= 1 }, 1000)</script>
</body>
</html>`

const SMALL_SNIPPET = 'const x = 1'

function fenced(language: string, body: string): string {
  return `Here you go:\n\n\`\`\`${language}\n${body}\n\`\`\`\n`
}

// End-to-end for the html fence path: a ```html fence in assistant markdown
// must come out of preprocessMarkdown -> Streamdown -> SyntaxHighlighter as an
// inline sandboxed iframe in the message itself — not an artifact card, not a
// plain code block. Non-html fences keep their existing paths (code card for
// small snippets, artifact card for substantial code).
describe('MarkdownTextContent html fences', () => {
  beforeEach(() => {
    $activeSessionId.set('session-artifacts')
    $selectedStoredSessionId.set(null)
    window.localStorage.clear()
    clearArtifactRegistry()
  })

  afterEach(() => {
    cleanup()
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    clearArtifactRegistry()
    window.localStorage.clear()
  })

  it('renders a substantial html fence as an inline sandboxed iframe (not a card)', async () => {
    const { container } = render(<MarkdownTextContent isRunning={false} text={fenced('html', HTML_DOC)} />)

    const frame = await screen.findByTitle('Pomodoro Timer')

    expect(frame.tagName).toBe('IFRAME')
    expect(frame.getAttribute('sandbox')).toContain('allow-scripts')
    expect(frame.getAttribute('srcdoc')).toContain('<h1>Pomodoro</h1>')
    expect(container.querySelector('[data-slot="aui_artifact-card"]')).toBeNull()
    expect(container.querySelector('[data-slot="code-card"]')).toBeNull()
    expect(artifactsForSession('session-artifacts')).toHaveLength(0)
    // Inline rendering must not open the right rail (offer, don't hijack).
    expect($previewTabs.get()).toHaveLength(0)
  })

  it('wraps an html fragment in a document shell with the height-sync shim', async () => {
    const { container } = render(
      <MarkdownTextContent isRunning={false} text={fenced('html', '<h1>Frag</h1><p>no shell here</p>')} />
    )

    const frame = await screen.findByTitle('Frag')
    const srcDoc = frame.getAttribute('srcdoc') ?? ''

    expect(srcDoc).toMatch(/<!doctype html>/i)
    expect(srcDoc).toContain('__inlineHeight')
    expect(container.querySelector('[data-slot="aui_artifact-card"]')).toBeNull()
  })

  it('keeps a small non-html fence as a plain code block', async () => {
    const { container } = render(<MarkdownTextContent isRunning={false} text={fenced('js', SMALL_SNIPPET)} />)

    // The code card mounts synchronously; Shiki may split tokens into spans,
    // so assert on the card slots rather than text content.
    expect(container.querySelector('[data-slot="code-card"]')).not.toBeNull()
    expect(container.querySelector('[data-slot="aui_artifact-card"]')).toBeNull()
    expect(container.querySelector('iframe')).toBeNull()
    expect(artifactsForSession('session-artifacts')).toHaveLength(0)
  })

  it('shows a placeholder while the message is streaming, then no registration', async () => {
    const { container } = render(<MarkdownTextContent isRunning text={fenced('html', HTML_DOC)} />)

    // Streaming fences defer the iframe; only the shimmer placeholder exists.
    expect(container.querySelector('[data-slot="inline-html-placeholder"]')).not.toBeNull()
    expect(container.querySelector('iframe')).toBeNull()
    expect(artifactsForSession('session-artifacts')).toHaveLength(0)
  })
})
