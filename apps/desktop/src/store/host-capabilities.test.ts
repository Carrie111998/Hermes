import { afterEach, describe, expect, it } from 'vitest'

import {
  $hostCapabilities,
  activateHostCapabilities,
  clearHostCapabilities,
  hostCapabilityScope,
  ingestHostCapabilities,
  resetHostCapabilities
} from './host-capabilities'

const descriptor = {
  'mcp-client-access': {
    endpoints: ['mcp.client.status', 'mcp.client.tools', 'mcp.client.call'],
    version: { major: 1, minor: 0 }
  }
}

describe('host capability handshake state', () => {
  afterEach(resetHostCapabilities)

  it('accepts a bounded versioned descriptor from gateway.ready', () => {
    ingestHostCapabilities(descriptor)

    expect($hostCapabilities.get()).toEqual(descriptor)
    expect($hostCapabilities.get()).not.toBe(descriptor)
  })

  it('isolates cached descriptors by connection and profile on backend switches', () => {
    const sourceA = hostCapabilityScope('source-a', 'researcher')
    const sourceB = hostCapabilityScope('source-b', 'researcher')

    ingestHostCapabilities(descriptor, sourceA)
    activateHostCapabilities(sourceA)
    expect($hostCapabilities.get()).toEqual(descriptor)

    activateHostCapabilities(sourceB)
    expect($hostCapabilities.get()).toEqual({})

    ingestHostCapabilities(
      {
        'mcp-client-access': {
          endpoints: ['mcp.client.status', 'mcp.client.tools', 'mcp.client.call'],
          version: { major: 1, minor: 1 }
        }
      },
      sourceB
    )
    expect($hostCapabilities.get()['mcp-client-access']?.version.minor).toBe(1)
  })

  it.each([
    null,
    [],
    { 'mcp-client-access': { endpoints: ['mcp.client.status'], version: { major: 1, minor: '0' } } },
    { 'mcp-client-access': { endpoints: ['mcp.client.status', 'mcp.client.status'], version: { major: 1, minor: 0 } } },
    { 'mcp-client-access': { endpoints: ['bad endpoint'], version: { major: 1, minor: 0 } } },
    { 'mcp-client-access': { endpoints: ['mcp.client.status'], extra: true, version: { major: 1, minor: 0 } } }
  ])('drops malformed capability payloads fail-closed', payload => {
    ingestHostCapabilities(payload)

    expect($hostCapabilities.get()).toEqual({})
  })

  it('clears backend capabilities before a reconnect can advertise replacements', () => {
    const scope = hostCapabilityScope('source-a', 'researcher')
    ingestHostCapabilities(descriptor, scope)
    activateHostCapabilities(scope)
    clearHostCapabilities(scope)

    expect($hostCapabilities.get()).toEqual({})
  })
})
