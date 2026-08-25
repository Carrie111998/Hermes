import { describe, expect, it } from 'vitest'
import { getServers, isServerShape } from '@/lib/mcp-servers'

describe('mcp-servers edge cases', () => {
  it('handles nested transport values', () => {
    const config = {
      mcp_servers: {
        a: { transport: 'stdio', command: 'npx', args: ['--yes'] },
        b: { transport: 'sse', url: 'https://example.com/sse' },
      },
    }
    const servers = getServers(config)
    expect(Object.keys(servers)).toHaveLength(2)
    expect(isServerShape(servers.a)).toBe(true)
    expect(isServerShape(servers.b)).toBe(true)
  })

  it('isServerShape with non-string types', () => {
    expect(isServerShape({ command: 123 })).toBe(false)
    expect(isServerShape({ url: true })).toBe(false)
    expect(isServerShape({})).toBe(false)
  })

  it('normalizeEntry with type and transport both present', () => {
    const result = normalizeEntry({ type: 'old', transport: 'new', command: 'x' })
    expect(result.transport).toBe('new')
  })
})
