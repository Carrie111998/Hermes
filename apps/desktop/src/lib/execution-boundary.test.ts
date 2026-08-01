import { describe, expect, it } from 'vitest'

import { resolveExecutionBoundary } from './execution-boundary'

describe('resolveExecutionBoundary', () => {
  it('always identifies the local execution boundary', () => {
    expect(resolveExecutionBoundary(null)).toEqual({ host: null, kind: 'local' })
    expect(resolveExecutionBoundary({ mode: 'local' })).toEqual({ host: null, kind: 'local' })
  })

  it('identifies URL, SSH, and Cloud remote boundaries with their host', () => {
    expect(resolveExecutionBoundary({ mode: 'remote', remoteHost: 'mosquito-nas.local', remoteKind: 'url' })).toEqual({
      host: 'mosquito-nas.local',
      kind: 'remote'
    })
    expect(resolveExecutionBoundary({ mode: 'remote', remoteHost: 'operator@nas', remoteKind: 'ssh' })).toEqual({
      host: 'operator@nas',
      kind: 'ssh'
    })
    expect(resolveExecutionBoundary({ mode: 'remote', remoteHost: 'agent.example', remoteKind: 'cloud' })).toEqual({
      host: 'agent.example',
      kind: 'cloud'
    })
  })

  it('still exposes an unknown remote boundary instead of falling back to local', () => {
    expect(resolveExecutionBoundary({ mode: 'remote' })).toEqual({ host: null, kind: 'remote' })
  })
})
