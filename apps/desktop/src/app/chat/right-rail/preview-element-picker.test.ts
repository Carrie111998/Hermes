import { describe, expect, it } from 'vitest'

import { formatPickedElement, isPickerResult, type PickedElement } from './preview-element-picker'

const picked: PickedElement = {
  accessibleName: 'Sign in',
  attributes: { href: '/login', id: 'cta' },
  htmlExcerpt: '<a id="cta" href="/login">Sign in</a>',
  nearbyText: 'Welcome Sign in',
  pageTitle: 'Example',
  pageUrl: 'https://example.com/',
  rect: { height: 40, width: 120, x: 10, y: 20 },
  role: 'link',
  selector: '#cta',
  style: { color: '#2563EB', fontFamily: 'Inter', fontSize: '14px' },
  tagName: 'a',
  text: 'Sign in',
  xpath: '/html[1]/body[1]/a[1]'
}

describe('formatPickedElement', () => {
  it('writes the ZCode web-element block the composer already understands', () => {
    const block = formatPickedElement(picked)

    expect(block).toContain('## Element')
    expect(block).toContain('URL: https://example.com/')
    expect(block).toContain('Tag: a')
    expect(block).toContain('Role: link')
    expect(block).toContain('Selector: #cta')
    expect(block).toContain('XPath: /html[1]/body[1]/a[1]')
    expect(block).toContain('```\nSign in\n```')
    expect(block).toContain('```html\n<a id="cta" href="/login">Sign in</a>\n```')
  })

  it('omits empty optional fields', () => {
    const block = formatPickedElement({
      pageTitle: '',
      pageUrl: 'https://example.com/',
      tagName: 'div'
    })

    expect(block).toContain('Title: (untitled)')
    expect(block).not.toContain('Role:')
    expect(block).not.toContain('Selector:')
    expect(block).not.toContain('```html')
  })
})

describe('isPickerResult', () => {
  it('accepts cancelled and selected payloads', () => {
    expect(isPickerResult({ status: 'cancelled' })).toBe(true)
    expect(isPickerResult({ status: 'selected', element: picked })).toBe(true)
    expect(isPickerResult({ status: 'selected' })).toBe(false)
    expect(isPickerResult(null)).toBe(false)
  })
})
