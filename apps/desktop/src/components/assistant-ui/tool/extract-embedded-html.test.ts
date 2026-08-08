import { describe, it, expect } from 'vitest'
import { extractEmbeddedHtml } from './fallback-model'

describe('extractEmbeddedHtml', () => {
  it('returns empty string for null/undefined', () => {
    expect(extractEmbeddedHtml(null)).toBe('')
    expect(extractEmbeddedHtml(undefined)).toBe('')
    expect(extractEmbeddedHtml('not an object')).toBe('')
  })

  it('returns empty string when content has no resource items', () => {
    expect(extractEmbeddedHtml({ content: [{ type: 'text', text: 'hello' }] })).toBe('')
    expect(extractEmbeddedHtml({ content: 'just a string' })).toBe('')
    expect(extractEmbeddedHtml({ content: [] })).toBe('')
  })

  it('extracts HTML from a content array with a resource item', () => {
    const result = {
      content: [
        { type: 'text', text: '{"balance":2.5}' },
        {
          type: 'resource',
          resource: {
            uri: 'ui://example/balance-card',
            mimeType: 'text/html',
            text: '<!DOCTYPE html><html><body>Balance Card</body></html>',
          },
        },
      ],
    }
    expect(extractEmbeddedHtml(result)).toBe('<!DOCTYPE html><html><body>Balance Card</body></html>')
  })

  it('returns empty string for non-HTML resources', () => {
    const result = {
      content: [
        {
          type: 'resource',
          resource: {
            uri: 'file://some/file.json',
            mimeType: 'application/json',
            text: '{"data": 1}',
          },
        },
      ],
    }
    expect(extractEmbeddedHtml(result)).toBe('')
  })

  it('returns empty string when resource text is empty', () => {
    const result = {
      content: [
        {
          type: 'resource',
          resource: {
            uri: 'ui://example/card',
            mimeType: 'text/html',
            text: '',
          },
        },
      ],
    }
    expect(extractEmbeddedHtml(result)).toBe('')
  })

  it('extracts HTML from a resource with additional unknown fields', () => {
    const result = {
      content: [
        { type: 'text', text: 'result text' },
        {
          type: 'resource',
          resource: {
            uri: 'ui://example/swap-card',
            mimeType: 'text/html',
            text: '<html><body>Swap Card</body></html>',
            blobs: ['extra-data'],
          },
        },
      ],
    }
    expect(extractEmbeddedHtml(result)).toBe('<html><body>Swap Card</body></html>')
  })

  it('returns empty string when content is missing', () => {
    expect(extractEmbeddedHtml({ success: true })).toBe('')
    expect(extractEmbeddedHtml({ data: { balance: 1 } })).toBe('')
    expect(extractEmbeddedHtml({})).toBe('')
  })
})