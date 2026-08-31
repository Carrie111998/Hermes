import { JsonRpcGatewayError } from '@hermes/shared'
import { afterEach, describe, expect, it } from 'vitest'

import {
  isSessionNotFoundError,
  isSessionRpcBlocked,
  markSessionRpcBlocked,
  resetSessionRpcGuard
} from './session-rpc-guard'

afterEach(() => {
  resetSessionRpcGuard()
})

describe('session-scoped RPC guard', () => {
  it('recognizes structured 4001 and legacy text errors without misclassifying coded errors', () => {
    expect(isSessionNotFoundError(new JsonRpcGatewayError('gone', { code: 4001 }))).toBe(true)
    expect(isSessionNotFoundError(new JsonRpcGatewayError('session not found', { code: 5007 }))).toBe(false)
    expect(isSessionNotFoundError(new JsonRpcGatewayError('session not found'))).toBe(true)
    expect(isSessionNotFoundError(new Error('tool failed: upstream said session not found'))).toBe(false)
  })

  it('blocks only the dead runtime until that runtime is explicitly rebound', () => {
    expect(isSessionRpcBlocked('s1')).toBe(false)

    markSessionRpcBlocked('s1')

    expect(isSessionRpcBlocked('s1')).toBe(true)
    expect(isSessionRpcBlocked('s2')).toBe(false)

    resetSessionRpcGuard('s1')

    expect(isSessionRpcBlocked('s1')).toBe(false)
  })
})
