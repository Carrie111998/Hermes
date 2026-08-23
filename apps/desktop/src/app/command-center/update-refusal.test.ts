import { describe, expect, it } from 'vitest'

import { terminalUpdateRefusal } from './update-refusal'

describe('terminalUpdateRefusal', () => {
  it('renders server guidance immediately without a synthetic PID', () => {
    const command = 'docker compose pull && docker compose up -d --force-recreate'

    const result = terminalUpdateRefusal({
      action_id: 'a'.repeat(32),
      error: 'image_managed_update_refused',
      message: 'This deployment is image-managed.',
      name: 'hermes-update',
      ok: false,
      pid: null,
      update_command: command
    })

    expect(result.guidance).toBe(`This deployment is image-managed.\n${command}`)
    expect(result.status).toEqual({
      action_id: 'a'.repeat(32),
      exit_code: 2,
      lines: ['This deployment is image-managed.', command],
      name: 'hermes-update',
      pid: null,
      running: false
    })
  })

  it('never invents hermes update when remediation is absent', () => {
    const result = terminalUpdateRefusal({
      error: 'image_managed_update_refused',
      message: 'Consult your deployment operator.',
      name: 'hermes-update',
      ok: false,
      pid: null
    })

    expect(result.guidance).toBe('Consult your deployment operator.')
    expect(result.guidance).not.toContain('hermes update')
    expect(result.status.lines).toEqual(['Consult your deployment operator.'])
  })

  it('rejects malformed JSON scalars instead of stringifying or executing them', () => {
    const result = terminalUpdateRefusal({
      action_id: { poisoned: true },
      error: 'image_managed_update_refused',
      message: { poisoned: true },
      name: ['hermes-update'],
      ok: false,
      pid: null,
      update_command: ['not', 'a', 'command']
    } as never)

    expect(result.guidance).toBe('Update not available for this backend.')
    expect(result.status).toEqual({
      action_id: undefined,
      exit_code: 2,
      lines: ['Update not available for this backend.'],
      name: 'hermes-update',
      pid: null,
      running: false
    })
  })
})
