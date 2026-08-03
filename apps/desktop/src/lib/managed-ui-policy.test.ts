import { describe, expect, it } from 'vitest'

import {
  isManagedBillingSlashCommand,
  isManagedConfigFieldVisible,
  isManagedSettingsViewVisible
} from './managed-ui-policy'

describe('managed renderer policy', () => {
  it('removes Billing without narrowing any other Settings destination', () => {
    expect(isManagedSettingsViewVisible('billing', true)).toBe(false)
    expect(isManagedSettingsViewVisible('providers', true)).toBe(true)
    expect(isManagedSettingsViewVisible('gateway', true)).toBe(true)
    expect(isManagedSettingsViewVisible('plugins', true)).toBe(true)
    expect(isManagedSettingsViewVisible('billing', false)).toBe(true)
  })

  it('hides exactly the approved Advanced fields', () => {
    const hidden = [
      'toolsets',
      'terminal.backend',
      'terminal.docker_image',
      'terminal.singularity_image',
      'terminal.modal_image',
      'terminal.daytona_image',
      'updates.non_interactive_local_changes'
    ]

    expect(hidden.every(key => !isManagedConfigFieldVisible(key, true))).toBe(true)
    expect(isManagedConfigFieldVisible('terminal.timeout', true)).toBe(true)
    expect(isManagedConfigFieldVisible('agent.max_turns', true)).toBe(true)
    expect(isManagedConfigFieldVisible('toolsets', false)).toBe(true)
  })

  it('denies only managed billing commands and their legacy alias', () => {
    for (const command of ['/topup', '/subscription', '/upgrade']) {
      expect(isManagedBillingSlashCommand(command, true)).toBe(true)
      expect(isManagedBillingSlashCommand(command, false)).toBe(false)
    }

    expect(isManagedBillingSlashCommand('/my-billing-skill', true)).toBe(false)
    expect(isManagedBillingSlashCommand('/tools', true)).toBe(false)
  })
})
