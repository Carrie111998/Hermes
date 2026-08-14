import { describe, expect, it } from 'vitest'

import { formatMcpLaunchTarget } from './mcp-launch-target'

describe('formatMcpLaunchTarget', () => {
  it('returns null when the source names no target', () => {
    expect(formatMcpLaunchTarget({})).toBeNull()
    expect(formatMcpLaunchTarget({ args: ['-y', 'pkg'] })).toBeNull()
    expect(formatMcpLaunchTarget({ command: '   ' })).toBeNull()
  })

  it('prefers the remote URL over a stdio command', () => {
    expect(
      formatMcpLaunchTarget({ args: ['server'], command: 'node', url: 'https://mcp.example.com/sse' })
    ).toEqual({ kind: 'remote', target: 'https://mcp.example.com/sse' })
  })

  it('joins command and args into one stdio line', () => {
    expect(formatMcpLaunchTarget({ args: ['-y', '@scope/server', '--port', '80'], command: 'npx' })).toEqual({
      kind: 'stdio',
      target: 'npx -y @scope/server --port 80'
    })
  })

  it('renders a bare command without args', () => {
    expect(formatMcpLaunchTarget({ command: 'my-server' })).toEqual({ kind: 'stdio', target: 'my-server' })
  })

  it('strips terminal control characters from workspace-supplied text', () => {
    const result = formatMcpLaunchTarget({
      args: ['\u001B[2Jrun\u0007', '--flag\u009B31m'],
      command: 'bash\u001B]0;pwned\u0007'
    })

    expect(result?.target).toBe('bash ]0;pwned [2Jrun --flag 31m')
    // eslint-disable-next-line no-control-regex
    expect(result?.target).not.toMatch(/[\u0000-\u001F\u007F-\u009F]/)
  })

  it('collapses newlines and repeated whitespace to a single line', () => {
    expect(formatMcpLaunchTarget({ command: 'node', args: ['a\n\nb', '  c  '] })?.target).toBe('node a b c')
  })

  it('elides overlong targets with an ellipsis', () => {
    const result = formatMcpLaunchTarget({ command: 'node', args: ['x'.repeat(400)] })

    expect(result?.target.length).toBeLessThanOrEqual(160)
    expect(result?.target.endsWith('…')).toBe(true)
  })

  it('sanitizes control characters inside URLs too', () => {
    expect(formatMcpLaunchTarget({ url: 'https://evil.test/\u001B[1mpath' })?.target).toBe(
      'https://evil.test/ [1mpath'
    )
  })
})
