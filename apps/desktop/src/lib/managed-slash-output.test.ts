import { describe, expect, it } from 'vitest'

import { sanitizeDesktopSlashOutput } from './managed-slash-output'

describe('sanitizeDesktopSlashOutput', () => {
  it('leaves unmanaged slash output byte-for-byte unchanged', () => {
    const output = 'Hermes Agent v0.9.1\nRun /subscription · https://portal.nousresearch.com'

    expect(sanitizeDesktopSlashOutput('/version', output, false)).toBe(output)
    expect(sanitizeDesktopSlashOutput('/usage', output, false)).toBe(output)
  })

  it('brands managed version output while preserving version detail', () => {
    expect(sanitizeDesktopSlashOutput('/version', 'Hermes Agent v0.9.1 (abcdef)', true)).toBe(
      'evaOS Agent v0.9.1 (abcdef)'
    )
  })

  it('replaces a managed free-account-only usage response with neutral session copy', () => {
    const output = [
      'Plan: Free',
      '> Free · free models only. Run /subscription to reach paid models.',
      'Run /subscription to change plan · /topup to add to your balance'
    ].join('\n')

    expect(sanitizeDesktopSlashOutput('/usage', output, true)).toBe('No session token usage is available yet.')
  })

  it('preserves low-usage session facts and removes account, balance, and CTA blocks', () => {
    const output = [
      '  📊 Session Token Usage',
      '  Model:                     hermes-3',
      '  Input tokens:                   1,234',
      '  Total tokens:                   1,500',
      '',
      '  📈 Account limits',
      '  Provider: nous (Pro)',
      '  Subscription: 95% remaining (5% used)',
      '',
      '  Plan: Plus · renews tomorrow',
      '  ! Low balance · $3.40 left. Run /topup or /subscription.'
    ].join('\n')

    const result = sanitizeDesktopSlashOutput('/usage', output, true)

    expect(result).toContain('📊 Session Token Usage')
    expect(result).toContain('Model:                     evaOS Agent-3')
    expect(result).toContain('Total tokens:')
    expect(result).not.toMatch(/nous|account limits|subscription|topup|balance|plan:/i)
  })

  it('preserves paid-session and rate-limit facts while dropping trailing portal account sections', () => {
    const output = [
      'Rate limits',
      'Requests: 50% remaining',
      '',
      '📊 Session Token Usage',
      'API calls: 7',
      'Current context: 12,000 / 64,000 (19%)',
      '',
      '📈 **Nous credits**',
      'Provider: nous (Max)',
      'Subscription credits: $110.00',
      'Top-up credits: $12.00',
      'Top up: https://portal.nousresearch.com/billing',
      '(or run /topup)'
    ].join('\n')

    const result = sanitizeDesktopSlashOutput('usage', output, true)

    expect(result).toContain('Rate limits')
    expect(result).toContain('Requests: 50% remaining')
    expect(result).toContain('API calls: 7')
    expect(result).not.toMatch(/nous|credits|topup|top-up|portal|billing/i)
  })

  it('does not alter unrelated managed slash commands', () => {
    expect(sanitizeDesktopSlashOutput('/status', 'Hermes Agent ready', true)).toBe('Hermes Agent ready')
  })
})
