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

  it('extracts HTML from a direct content array with a resource item', () => {
    const result = {
      content: [
        { type: 'text', text: '{"sol":2.5}' },
        {
          type: 'resource',
          resource: {
            uri: 'ui://sap/balance-card',
            mimeType: 'text/html',
            text: '<!DOCTYPE html><html><body>SAP Balance Card</body></html>',
          },
        },
      ],
    }
    expect(extractEmbeddedHtml(result)).toBe('<!DOCTYPE html><html><body>SAP Balance Card</body></html>')
  })

  it('extracts HTML from a wrapped success envelope', () => {
    const result = {
      success: true,
      tool: 'sol_get_balance',
      hostedPricing: 'free',
      data: {
        content: [
          { type: 'text', text: '{"sol":2.5}' },
          {
            type: 'resource',
            resource: {
              uri: 'ui://sap/balance-card',
              mimeType: 'text/html',
              text: '<html><body>Wrapped Card</body></html>',
            },
          },
        ],
      },
    }
    expect(extractEmbeddedHtml(result)).toBe('<html><body>Wrapped Card</body></html>')
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
            uri: 'ui://sap/balance-card',
            mimeType: 'text/html',
            text: '',
          },
        },
      ],
    }
    expect(extractEmbeddedHtml(result)).toBe('')
  })

  it('prefers direct content over wrapped data', () => {
    const result = {
      content: [
        {
          type: 'resource',
          resource: { mimeType: 'text/html', text: '<html>direct</html>' },
        },
      ],
      data: {
        content: [
          {
            type: 'resource',
            resource: { mimeType: 'text/html', text: '<html>wrapped</html>' },
          },
        ],
      },
    }
    expect(extractEmbeddedHtml(result)).toBe('<html>direct</html>')
  })
})