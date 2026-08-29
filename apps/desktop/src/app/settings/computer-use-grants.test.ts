import { afterEach, describe, expect, it } from 'vitest'

import { setApiRequestConnection } from '@/api/client'

import {
  peekComputerUseGrant,
  rememberComputerUseGrant,
  resetComputerUseGrantLedger
} from './computer-use-grants'

afterEach(() => {
  resetComputerUseGrantLedger()
  setApiRequestConnection(null)
})

describe('computer-use grant ledger', () => {
  it('does not treat an ambient remote default as a local grant', () => {
    setApiRequestConnection('homelab')
    rememberComputerUseGrant('default', 'computer-use-grant')

    expect(peekComputerUseGrant('default')).toBe('computer-use-grant')
    expect(peekComputerUseGrant({ connectionId: 'local', profile: 'default' })).toBeUndefined()
  })
})
