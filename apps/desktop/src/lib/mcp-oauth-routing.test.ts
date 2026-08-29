import { describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/hermes'

import { completeRoutedMcpDesktopOAuth, resolveMcpOAuthScope } from './mcp-oauth-routing'

const approved = {
  flow_id: 'flow-1',
  server_name: 'linear',
  status: 'approved' as const,
  authorization_url: null,
  error: null,
  tools: [{ name: 'list_issues', description: 'List issues' }]
}

describe('completeRoutedMcpDesktopOAuth', () => {
  it('preserves the ambient registry connection for legacy scopes', () => {
    expect(resolveMcpOAuthScope(undefined, 'studio')).toEqual({ connectionId: 'studio', profile: 'default' })
    expect(resolveMcpOAuthScope('work', 'studio')).toEqual({ connectionId: 'studio', profile: 'work' })
  })

  it('pins explicit object scopes without a remote connection to the local pool', () => {
    expect(resolveMcpOAuthScope({ profile: 'work' }, 'studio')).toEqual({ connectionId: 'local', profile: 'work' })
    expect(resolveMcpOAuthScope({ connectionId: 'local', profile: 'work' }, 'studio')).toEqual({
      connectionId: 'local',
      profile: 'work'
    })
  })

  it('uses the loopback gateway flow for a local backend', async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined)

    const requestRpc = vi
      .fn()
      .mockResolvedValueOnce({ session_id: 'flow-1', auth_url: 'https://linear.example/authorize' })
      .mockResolvedValueOnce({ status: 'approved', tools: approved.tools })

    const restStart = vi.fn()
    const restStatus = vi.fn()

    const result = await completeRoutedMcpDesktopOAuth(
      {
        serverName: 'linear',
        scope: { connectionId: 'local', profile: 'default' },
        openExternal,
        sleep: async () => {}
      },
      {
        connectionMode: async () => 'local',
        requestRpc,
        restStart,
        restStatus,
        restCancel: vi.fn()
      }
    )

    expect(requestRpc).toHaveBeenNthCalledWith(1, { connectionId: 'local', profile: 'default' }, 'mcp.servers.oauth.start', {
      name: 'linear'
    })
    expect(requestRpc).toHaveBeenNthCalledWith(2, { connectionId: 'local', profile: 'default' }, 'mcp.servers.oauth.poll', {
      name: 'linear',
      session_id: 'flow-1'
    })
    expect(restStart).not.toHaveBeenCalled()
    expect(restStatus).not.toHaveBeenCalled()
    expect(openExternal).toHaveBeenCalledWith('https://linear.example/authorize')
    expect(result).toEqual(approved)
  })

  it('keeps the dashboard callback flow for a remote backend', async () => {
    const restStart = vi.fn().mockResolvedValue({
      flow_id: 'flow-remote',
      server_name: 'linear',
      status: 'authorization_required',
      authorization_url: 'https://linear.example/authorize',
      error: null
    })

    const restStatus = vi.fn().mockResolvedValue({ ...approved, flow_id: 'flow-remote' })
    const requestRpc = vi.fn()

    const result = await completeRoutedMcpDesktopOAuth(
      {
        serverName: 'linear',
        scope: { connectionId: 'studio', profile: 'default' },
        openExternal: vi.fn().mockResolvedValue(undefined),
        sleep: async () => {}
      },
      {
        connectionMode: async () => 'remote',
        requestRpc,
        restStart,
        restStatus,
        restCancel: vi.fn()
      }
    )

    expect(requestRpc).not.toHaveBeenCalled()
    expect(restStart).toHaveBeenCalledWith('linear', { connectionId: 'studio', profile: 'default' })
    expect(restStatus).toHaveBeenCalledWith('flow-remote', { connectionId: 'studio', profile: 'default' })
    expect(result.flow_id).toBe('flow-remote')
  })

  it('cancels a local loopback flow through the gateway', async () => {
    const requestRpc = vi
      .fn()
      .mockResolvedValueOnce({ session_id: 'flow-cancel', auth_url: 'https://linear.example/authorize' })
      .mockResolvedValueOnce({ status: 'error' })

    await expect(
      completeRoutedMcpDesktopOAuth(
        {
          serverName: 'linear',
          cancelled: () => true,
          openExternal: vi.fn().mockResolvedValue(undefined)
        },
        {
          connectionMode: async () => 'local',
          requestRpc,
          restStart: vi.fn(),
          restStatus: vi.fn(),
          restCancel: vi.fn()
        }
      )
    ).rejects.toMatchObject({ name: 'McpOAuthCancelled' })

    expect(requestRpc).toHaveBeenNthCalledWith(
      2,
      { connectionId: 'local', profile: 'default' },
      'mcp.servers.oauth.cancel',
      { name: 'linear', session_id: 'flow-cancel' }
    )
  })

  it('freezes an ambient route before the connection changes', async () => {
    const restStart = vi.fn().mockImplementation(async (_name, scope) => {
      setApiRequestConnection('other')

      return {
        flow_id: 'flow-frozen',
        server_name: 'linear',
        status: 'authorization_required',
        authorization_url: 'https://linear.example/authorize',
        error: null,
        scope
      }
    })

    const restStatus = vi.fn().mockResolvedValue({ ...approved, flow_id: 'flow-frozen' })

    setApiRequestConnection('studio')

    try {
      await completeRoutedMcpDesktopOAuth(
        {
          serverName: 'linear',
          openExternal: vi.fn().mockResolvedValue(undefined),
          sleep: async () => {}
        },
        {
          connectionMode: async () => 'remote',
          requestRpc: vi.fn(),
          restStart,
          restStatus,
          restCancel: vi.fn()
        }
      )
    } finally {
      setApiRequestConnection(null)
    }

    const frozen = { connectionId: 'studio', profile: 'default' }
    expect(restStart).toHaveBeenCalledWith('linear', frozen)
    expect(restStatus).toHaveBeenCalledWith('flow-frozen', frozen)
  })

  it('preserves the exact remote scope when cancelling', async () => {
    const scope = { connectionId: 'studio', profile: 'work' }
    const restCancel = vi.fn().mockResolvedValue(undefined)

    await expect(
      completeRoutedMcpDesktopOAuth(
        {
          serverName: 'linear',
          scope,
          cancelled: () => true,
          openExternal: vi.fn().mockResolvedValue(undefined)
        },
        {
          connectionMode: async () => 'remote',
          requestRpc: vi.fn(),
          restStart: vi.fn().mockResolvedValue({
            flow_id: 'flow-remote-cancel',
            server_name: 'linear',
            status: 'authorization_required',
            authorization_url: 'https://linear.example/authorize',
            error: null
          }),
          restStatus: vi.fn(),
          restCancel
        }
      )
    ).rejects.toMatchObject({ name: 'McpOAuthCancelled' })

    expect(restCancel).toHaveBeenCalledWith('flow-remote-cancel', scope)
  })
})
