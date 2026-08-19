import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  type ExtractedPreviewPage,
  PREVIEW_PAGE_EXTRACT_SCRIPT,
  PREVIEW_SELECTION_TEXT_MAX_CHARS,
  PREVIEW_VISIBLE_HEADING_LIMIT,
  PREVIEW_VISIBLE_HEADING_TEXT_MAX_CHARS,
  PREVIEW_VISIBLE_TEXT_MAX_CHARS
} from './preview-page-extract'

function rect(left: number, top: number, right: number, bottom: number): DOMRect {
  return {
    bottom,
    height: bottom - top,
    left,
    right,
    toJSON: () => ({}),
    top,
    width: right - left,
    x: left,
    y: top
  }
}

function place(element: Element, box: DOMRect): void {
  element.getBoundingClientRect = () => box
}

describe('preview page viewport extraction', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 500 })
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 800 })
    Object.defineProperty(window, 'scrollY', { configurable: true, value: 750 })
    Object.defineProperty(document.documentElement, 'scrollHeight', { configurable: true, value: 2_500 })
  })

  it('extracts only viewport-intersecting headings and text, including nested clipping', () => {
    document.body.innerHTML = `
      <h2 id="above">Above</h2>
      <h2 id="visible">Current section</h2>
      <p id="visible-copy">Visible explanation</p>
      <div id="scroller" style="overflow: auto"><p id="clipped">Clipped nested copy</p></div>
    `
    place(document.querySelector('#above')!, rect(0, -100, 300, -50))
    place(document.querySelector('#visible')!, rect(0, 20, 300, 60))
    place(document.querySelector('#visible-copy')!, rect(0, 80, 400, 120))
    place(document.querySelector('#scroller')!, rect(0, 150, 400, 250))
    place(document.querySelector('#clipped')!, rect(0, 300, 400, 340))
    vi.spyOn(window, 'getSelection').mockReturnValue({ toString: () => ' selected words ' } as Selection)

    // Execute the exact JavaScript string sent to webview.executeJavaScript.
    const result = Function(`return ${PREVIEW_PAGE_EXTRACT_SCRIPT}`)() as ExtractedPreviewPage

    expect(result).toMatchObject({
      scroll_height: 2_500,
      scroll_ratio: 0.375,
      scroll_y: 750,
      selection_text: 'selected words',
      viewport_height: 500,
      visible_headings: [{ level: 2, text: 'Current section' }]
    })
    expect(result.visible_text).toContain('Current section')
    expect(result.visible_text).toContain('Visible explanation')
    expect(result.visible_text).not.toContain('Above')
    expect(result.visible_text).not.toContain('Clipped nested copy')
  })

  it('excludes text clipped by the element that directly owns it', () => {
    document.body.innerHTML = '<div id="clip" style="overflow: hidden">Direct clipped copy</div>'
    const clip = document.querySelector('#clip')!

    place(clip, rect(0, 100, 400, 150))
    vi.spyOn(document, 'createRange').mockReturnValue({
      detach: vi.fn(),
      getClientRects: () => [rect(0, 200, 400, 220)],
      selectNodeContents: vi.fn()
    } as unknown as Range)

    const result = Function(`return ${PREVIEW_PAGE_EXTRACT_SCRIPT}`)() as ExtractedPreviewPage

    expect(result.visible_text).not.toContain('Direct clipped copy')
  })

  it('hard-caps every variable-length viewport field in the guest payload', () => {
    const headings = Array.from({ length: PREVIEW_VISIBLE_HEADING_LIMIT + 4 }, (_, index) => {
      const heading = document.createElement('h2')

      heading.textContent = `${index}-${'h'.repeat(PREVIEW_VISIBLE_HEADING_TEXT_MAX_CHARS + 40)}`
      place(heading, rect(0, 10 + index, 400, 30 + index))
      document.body.append(heading)

      return heading
    })

    const paragraph = document.createElement('p')

    paragraph.textContent = 'v'.repeat(PREVIEW_VISIBLE_TEXT_MAX_CHARS + 500)
    place(paragraph, rect(0, 100, 400, 140))
    document.body.append(paragraph)
    vi.spyOn(window, 'getSelection').mockReturnValue({
      toString: () => 's'.repeat(PREVIEW_SELECTION_TEXT_MAX_CHARS + 500)
    } as Selection)

    const result = Function(`return ${PREVIEW_PAGE_EXTRACT_SCRIPT}`)() as ExtractedPreviewPage

    expect(headings).toHaveLength(PREVIEW_VISIBLE_HEADING_LIMIT + 4)
    expect(result.visible_headings).toHaveLength(PREVIEW_VISIBLE_HEADING_LIMIT)
    expect(result.visible_headings[0]?.text.length).toBe(PREVIEW_VISIBLE_HEADING_TEXT_MAX_CHARS)
    expect(result.visible_text.length).toBeLessThanOrEqual(PREVIEW_VISIBLE_TEXT_MAX_CHARS)
    expect(result.selection_text).toHaveLength(PREVIEW_SELECTION_TEXT_MAX_CHARS)
  })
})
