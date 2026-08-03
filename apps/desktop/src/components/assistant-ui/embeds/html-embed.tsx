'use client'

import { useEffect, useMemo, useRef, useState } from 'react'

import { composeHtmlDocument } from '@/lib/html-shell'

import type { RichFenceProps } from './types'

// Fences that render inline as a sandboxed iframe in the message itself
// (instead of promoting to an artifact card that opens in the right rail).
export const HTML_INLINE_FENCE_LANGUAGES: ReadonlySet<string> = new Set(['html', 'htm', 'xhtml'])

export const INLINE_HEIGHT_MESSAGE = '__inlineHeight'
const START_HEIGHT = 340
const MIN_HEIGHT = 60
// Cap on the iframe's reported content height. The frame's own allowed script
// controls the height message, so the reported value is untrusted — without a
// cap, a generated document could post an arbitrarily large number and blow up
// the transcript row. Content taller than this scrolls inside the frame.
export const MAX_HEIGHT = 1200

/** Height-sync shim injected into the composed document: reports the content
 *  height to the parent frame so the iframe sizes itself to its content. The
 *  message column has no fixed pane to fill (unlike the right rail), so the
 *  embed must grow with the document. */
const HEIGHT_SYNC_SCRIPT = `<script>(function(){
  var send = function () {
    try { parent.postMessage({ ${INLINE_HEIGHT_MESSAGE}: document.documentElement.scrollHeight }, '*') } catch (e) {}
  };
  send();
  if (window.ResizeObserver) { new ResizeObserver(send).observe(document.documentElement) }
  window.addEventListener('load', send);
  setTimeout(send, 250);
})()</script>`

function buildSrcDoc(code: string): string {
  const doc = composeHtmlDocument(code)

  return /<\/body>/i.test(doc) ? doc.replace(/<\/body>/i, `${HEIGHT_SYNC_SCRIPT}</body>`) : doc + HEIGHT_SYNC_SCRIPT
}

/** Human title for the frame's a11y label, derived from the document's own
 *  <title> or first <h1> — mirrors the artifact preview pane's title logic. */
function artifactTitle(code: string): string {
  const head = code.slice(0, 2000)
  const tag = /<title[^>]*>([\s\S]*?)<\/title>/i.exec(head)?.[1] || /<h1[^>]*>([\s\S]*?)<\/h1>/i.exec(head)?.[1] || ''

  return tag
    .replace(/<[^>]*>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 80) || 'HTML'
}

/**
 * Inline renderer for ```html / ```htm / ```xhtml fences: a sandboxed iframe
 * (opaque origin, scripts allowed, no same-origin escape) that auto-sizes to
 * its content height. Mirrors the artifact preview pane's html contract —
 * foreign generated HTML assumes a light canvas, so the frame is forced light
 * with a white background in both app themes.
 *
 * While the message is still streaming, a shimmer placeholder stands in; the
 * iframe mounts once the fence settles so the document isn't remounted as
 * tokens arrive.
 */
export function InlineHtmlEmbed({ code, streaming }: RichFenceProps) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null)
  const [height, setHeight] = useState(START_HEIGHT)
  const srcDoc = useMemo(() => buildSrcDoc(code), [code])

  // Content-height sync: only accept height reports from this frame's own
  // contentWindow, and clamp the reported value. The message is authored by
  // the untrusted document's script, so event.source checks provenance but
  // not honesty — the clamp is the actual security boundary.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.source !== iframeRef.current?.contentWindow) {
        return
      }

      const next = (event.data as Record<string, unknown> | null)?.[INLINE_HEIGHT_MESSAGE]

      if (typeof next === 'number' && Number.isFinite(next)) {
        const clamped = Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.round(next)))
        setHeight(clamped)
      }
    }

    window.addEventListener('message', onMessage)

    return () => window.removeEventListener('message', onMessage)
  }, [])

  if (streaming) {
    return (
      <div
        className="my-2 h-24 animate-pulse rounded-[0.375rem] border border-(--ui-stroke-tertiary) bg-muted/40"
        data-slot="inline-html-placeholder"
      />
    )
  }

  // Three-level scrollbar containment:
  // 1. srcdoc body constrains children (fragment shell CSS) + the height-sync
  //    shim grows the frame, so no inner scrollbar;
  // 2. the iframe element is display:block width:100% with explicit height
  //    (clamped to MAX_HEIGHT — taller documents scroll inside the frame);
  // 3. the container clips overflow and contains layout.
  return (
    <div
      className="not-prose my-2 overflow-hidden rounded-[0.375rem] border border-(--ui-stroke-tertiary)"
      style={{ maxWidth: '100%', width: '100%', boxSizing: 'border-box', contain: 'layout style' }}
    >
      <iframe
        className="block w-full border-0 bg-white"
        ref={iframeRef}
        sandbox="allow-scripts"
        srcDoc={srcDoc}
        style={{ height, colorScheme: 'light' }}
        title={artifactTitle(code)}
      />
    </div>
  )
}
