import { describe, expect, it } from 'vitest'

import {
  assertManagedGatewayMethodAllowed,
  isManagedBillingSlashCommand,
  isManagedConfigFieldVisible,
  isManagedSettingsViewVisible,
  isManagedTerminalUiVisible
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

  it('omits terminal UI entry points only for managed builds', () => {
    expect(isManagedTerminalUiVisible(true)).toBe(false)
    expect(isManagedTerminalUiVisible(false)).toBe(true)
  })

  it('denies only managed billing commands and their legacy alias', () => {
    for (const command of ['/topup', '/subscription', '/upgrade']) {
      expect(isManagedBillingSlashCommand(command, true)).toBe(true)
      expect(isManagedBillingSlashCommand(command, false)).toBe(false)
    }

    expect(isManagedBillingSlashCommand('/my-billing-skill', true)).toBe(false)
    expect(isManagedBillingSlashCommand('/tools', true)).toBe(false)
  })

  it('rejects Nous billing and subscription RPCs below the managed UI', () => {
    for (const method of [
      'billing.state',
      'billing.charge',
      'billing.auto_reload',
      'billing.step_up',
      'subscription.state',
      'subscription.change',
      'subscription.resume',
      'subscription.upgrade',
      'usage.bars'
    ]) {
      expect(() => assertManagedGatewayMethodAllowed(method, true)).toThrow(/unavailable in managed evaOS Agent/)
      expect(() => assertManagedGatewayMethodAllowed(method, false)).not.toThrow()
    }

    expect(() => assertManagedGatewayMethodAllowed('session.status', true)).not.toThrow()
    expect(() => assertManagedGatewayMethodAllowed('usage.snapshot', true)).not.toThrow()
  })
})
