import { describe, expect, it } from 'vitest'
import { isServerShape, normalizeEntry, getServers } from '@/lib/mcp-servers'

describe('isServerShape', () => {
  it('accepts command-based entries', () => {
    expect(isServerShape({ command: 'npx', args: [] })).toBe(true)
  })

  it('accepts url-based entries', () => {
    expect(isServerShape({ url: 'https://example.com' })).toBe(true)
  })

  it('rejects entries without command or url', () => {
    expect(isServerShape({ foo: 'bar' })).toBe(false)
  })
})

describe('normalizeEntry', () => {
  it('maps type to transport', () => {
    const result = normalizeEntry({ type: 'stdio', command: 'npx' })
    expect(result.transport).toBe('stdio')
    expect((result as Record<string, unknown>).type).toBeUndefined()
  })

  it('preserves existing transport', () => {
    const result = normalizeEntry({ transport: 'sse', url: 'https://x' })
    expect(result.transport).toBe('sse')
  })

  it('passes through entries without type', () => {
    const entry = { command: 'npx' }
    expect(normalizeEntry(entry)).toEqual(entry)
  })
})

describe('getServers', () => {
  it('extracts mcp_servers from config', () => {
    const config = { mcp_servers: { myserver: { command: 'npx' } } }
    const servers = getServers(config)
    expect(servers.myserver).toBeDefined()
  })

  it('returns empty object for null config', () => {
    expect(getServers(null)).toEqual({})
  })

  it('returns empty object for missing mcp_servers', () => {
    expect(getServers({})).toEqual({})
  })

  it('returns empty object for array mcp_servers', () => {
    expect(getServers({ mcp_servers: [] })).toEqual({})
  })
})
