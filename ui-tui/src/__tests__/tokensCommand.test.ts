import { beforeEach, describe, expect, it, vi } from 'vitest'

import { coreCommands } from '../app/slash/commands/core.js'

const tokensCommand = coreCommands.find(cmd => cmd.name === 'tokens')!

const guarded =
  <T>(fn: (r: T) => void) =>
  (r: null | T) => {
    if (r) {
      fn(r)
    }
  }

/**
 * Build a ctx whose config.set either resolves or rejects, so the command's
 * honesty about persistence can be observed from the transcript.
 */
const buildCtx = (rpcResult: { reject?: unknown; resolve?: unknown }) => {
  const sys = vi.fn()
  const guardedErr = vi.fn()

  const rpc = vi.fn(() =>
    'reject' in rpcResult ? Promise.reject(rpcResult.reject) : Promise.resolve(rpcResult.resolve)
  )

  const ctx = {
    gateway: { rpc },
    guarded,
    guardedErr,
    sid: 'sid-1',
    stale: () => false,
    transcript: { page: vi.fn(), sys },
    ui: { showTokens: false }
  }

  const run = async (arg: string) => {
    tokensCommand.run(arg, ctx as any, `/tokens${arg ? ` ${arg}` : ''}`)
    await Promise.allSettled(rpc.mock.results.map(r => r.value))
    await new Promise(resolve => setTimeout(resolve, 0))
  }

  return { ctx, guardedErr, rpc, run, sys }
}

const printed = (sys: ReturnType<typeof vi.fn>) => sys.mock.calls.map(c => c[0]).join('\n')

describe('/tokens slash command', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('/tokens always persists via config.set show_message_tokens', async () => {
    const { rpc, run } = buildCtx({ resolve: { key: 'show_message_tokens', value: 'on' } })

    await run('always')

    expect(rpc).toHaveBeenCalledWith('config.set', { key: 'show_message_tokens', value: 'on' })
  })

  it('/tokens always claims "(saved)" only after the gateway confirms', async () => {
    const { run, sys } = buildCtx({ resolve: { key: 'show_message_tokens', value: 'on' } })

    await run('always')

    expect(printed(sys)).toContain('tokens always (saved)')
  })

  // The bug: an unhandled config.set key rejects, the caller swallowed it, and
  // /tokens always still reported a persistence that vanished on restart.
  it('/tokens always does not claim "(saved)" when the gateway rejects', async () => {
    const { guardedErr, run, sys } = buildCtx({ reject: new Error('unknown config key: show_message_tokens') })

    await run('always')

    expect(printed(sys)).not.toContain('saved')
    expect(guardedErr).toHaveBeenCalledTimes(1)
  })

  it('/tokens off surfaces a rejected write instead of swallowing it', async () => {
    const { guardedErr, run } = buildCtx({ reject: new Error('unknown config key: show_message_tokens') })

    await run('off')

    expect(guardedErr).toHaveBeenCalledTimes(1)
  })

  it('/tokens on stays session-only and issues no config.set', async () => {
    const { rpc, run, sys } = buildCtx({ resolve: {} })

    await run('on')

    expect(rpc).not.toHaveBeenCalled()
    expect(printed(sys)).toContain('tokens on (this session)')
  })

  it('bare /tokens reports the current session state without writing', async () => {
    const { rpc, run, sys } = buildCtx({ resolve: {} })

    await run('')

    expect(rpc).not.toHaveBeenCalled()
    expect(printed(sys)).toContain('tokens off')
  })

  it('rejects unknown subcommands with usage text', async () => {
    const { rpc, run, sys } = buildCtx({ resolve: {} })

    await run('banana')

    expect(rpc).not.toHaveBeenCalled()
    expect(printed(sys)).toContain('usage: /tokens [on|off|always|status]')
  })
})
