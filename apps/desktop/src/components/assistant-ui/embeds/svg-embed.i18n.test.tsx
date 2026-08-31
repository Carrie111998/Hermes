/**
 * The sanitizer must not eat non-Latin content.
 *
 * The desktop hint tells the model a ```svg fence renders inline, and this
 * user's deliverables are Arabic. DOMPurify's svg profile strips scripts,
 * handlers and foreignObject — the question is whether Arabic, Chinese and
 * bidi text survive that pass intact, and whether the dangerous parts still
 * do not. A sanitizer that silently drops the text renders an empty box and
 * nobody finds out until a diagram ships blank.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import SvgRenderer from './svg-embed'

afterEach(cleanup)

const wrap = (inner: string) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 60">${inner}</svg>`

describe('SvgRenderer with non-Latin content', () => {
  it('keeps Arabic, Chinese and mixed bidi text', () => {
    const view = render(
      <SvgRenderer
        code={wrap(
          '<text x="4" y="20">تقرير المبيعات ٢٠٢٦</text>' +
            '<text x="4" y="40">接收订单</text>' +
            '<text x="4" y="55">مشروع Hermes v0.20.6</text>'
        )}
      />
    )

    const text = view.container.textContent ?? ''

    expect(text).toContain('تقرير المبيعات ٢٠٢٦')
    expect(text).toContain('接收订单')
    // Mixed direction in one string: the Latin run must survive beside Arabic.
    expect(text).toContain('مشروع Hermes v0.20.6')
  })

  it('preserves an explicit rtl direction rather than stripping it', () => {
    const view = render(
      <SvgRenderer code={wrap('<text x="4" y="20" direction="rtl">نص من اليمين</text>')} />
    )

    expect(view.container.querySelector('text')?.getAttribute('direction')).toBe('rtl')
  })

  it('still strips the dangerous parts around that text', () => {
    const view = render(
      <SvgRenderer
        code={wrap(
          '<script>window.__pwned = 1</script>' +
            '<text x="4" y="20" onclick="window.__pwned = 1">نص عربي</text>' +
            '<foreignObject><body xmlns="http://www.w3.org/1999/xhtml">html</body></foreignObject>'
        )}
      />
    )

    const html = view.container.innerHTML

    expect(view.container.textContent).toContain('نص عربي')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('onclick')
    expect(html.toLowerCase()).not.toContain('foreignobject')
  })

  it('renders nothing when sanitising leaves nothing behind', () => {
    const view = render(<SvgRenderer code="<script>1</script>" />)

    expect(view.container.innerHTML).toBe('')
  })
})
