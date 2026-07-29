'use strict'

/**
 * Tests for apps/desktop/electron/venv-blocker-scan.ts
 *
 * Run with: npx vitest run electron/venv-blocker-scan.test.ts
 * (from apps/desktop; wired into npm test:desktop:platforms)
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { describe, it } from 'vitest'

import {
  formatBlockerMessage,
  formatProbeFailedMessage,
  parseVenvBlockerScanOutput,
  reapVenvOrphans,
  resolveVenvPython,
  scanVenvBlockers
} from './venv-blocker-scan'
import type { ScanOutcome } from './venv-blocker-scan'

// ---------------------------------------------------------------------------
// resolveVenvPython
// ---------------------------------------------------------------------------

describe('resolveVenvPython', () => {
  it('returns a real path when a temp venv python file exists', () => {
    const sandbox = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-vt-'))

    try {
      const scriptsDir = process.platform === 'win32' ? 'Scripts' : 'bin'
      const pythonName = process.platform === 'win32' ? 'python.exe' : 'python3'
      const dir = path.join(sandbox, 'venv', scriptsDir)
      fs.mkdirSync(dir, { recursive: true })
      const pyPath = path.join(dir, pythonName)
      fs.writeFileSync(pyPath, '', { mode: 0o755 })
      assert.equal(resolveVenvPython(sandbox), pyPath)
    } finally {
      fs.rmSync(sandbox, { recursive: true, force: true })
    }
  })

  it('returns null for non-existent venv', () => {
    assert.equal(resolveVenvPython('/nonexistent'), null)
  })
})

// ---------------------------------------------------------------------------
// formatBlockerMessage / formatProbeFailedMessage
// ---------------------------------------------------------------------------

describe('formatBlockerMessage', () => {
  it('includes PID, name, cmdline, remote-client warning, and retry suggestion', () => {
    const msg = formatBlockerMessage({
      blocked: true,
      processes: [{ pid: 101, name: 'python.exe', cmdline: 'serve --host 10.0.0.1' }]
    })

    assert.ok(msg.includes('PID 101'))
    assert.ok(msg.includes('python.exe'))
    assert.ok(msg.includes('serve'))
    assert.ok(msg.includes('remote backend'))
    assert.ok(msg.includes('retry'))
    assert.ok(!msg.includes('force-venv'))
  })
})

describe('formatProbeFailedMessage', () => {
  it('suggests retry and hermes update', () => {
    const msg = formatProbeFailedMessage()
    assert.ok(msg.includes('hermes update'))
    assert.ok(msg.includes('retry'))
  })
})

// ---------------------------------------------------------------------------
// parseVenvBlockerScanOutput — pure function
// ---------------------------------------------------------------------------

describe('parseVenvBlockerScanOutput', () => {
  const ok = (over: any = {}) => JSON.stringify({ ok: true, blocked: false, processes: [], ...over })

  it('valid clear', () => {
    const o = parseVenvBlockerScanOutput(ok())
    assert.equal(o.kind, 'clear')
  })

  it('valid blocked', () => {
    const o = parseVenvBlockerScanOutput(
      ok({
        blocked: true,
        processes: [{ pid: 1, name: 'p', cmdline: 'c' }]
      })
    )

    assert.equal(o.kind, 'blocked')
  })

  it('malformed JSON', () => {
    assert.equal(parseVenvBlockerScanOutput('not json').kind, 'probe-failure')
  })

  it('ok=false is rejected', () => {
    assert.equal(
      parseVenvBlockerScanOutput(JSON.stringify({ ok: false, blocked: false, processes: [] })).kind,
      'probe-failure'
    )
  })

  it('blocked must be boolean', () => {
    assert.equal(parseVenvBlockerScanOutput(ok({ blocked: 'false' })).kind, 'probe-failure')
  })

  it('blocked=true with empty processes rejected', () => {
    assert.equal(parseVenvBlockerScanOutput(ok({ blocked: true, processes: [] })).kind, 'probe-failure')
  })

  it('blocked=false with non-empty processes rejected', () => {
    assert.equal(
      parseVenvBlockerScanOutput(ok({ processes: [{ pid: 1, name: 'p', cmdline: 'c' }] })).kind,
      'probe-failure'
    )
  })

  it('process pid must be positive integer', () => {
    assert.equal(
      parseVenvBlockerScanOutput(ok({ blocked: true, processes: [{ pid: 0, name: 'p', cmdline: 'c' }] })).kind,
      'probe-failure'
    )
  })

  it('process name must be non-empty string', () => {
    assert.equal(
      parseVenvBlockerScanOutput(ok({ blocked: true, processes: [{ pid: 1, name: '', cmdline: 'c' }] })).kind,
      'probe-failure'
    )
  })

  it('process missing cmdline is rejected', () => {
    assert.equal(
      parseVenvBlockerScanOutput(ok({ blocked: true, processes: [{ pid: 1, name: 'p' }] })).kind,
      'probe-failure'
    )
  })
})

// ---------------------------------------------------------------------------
// scanVenvBlockers — subprocess with injection
// ---------------------------------------------------------------------------

describe('scanVenvBlockers', () => {
  const stubVenv = () => '/fake/venv/python.exe'
  const okJson = JSON.stringify({ ok: true, blocked: false, processes: [] })

  const blockedJson = JSON.stringify({
    ok: true,
    blocked: true,
    processes: [{ pid: 1, name: 'p', cmdline: 'c' }]
  })

  function execReturn(json: string): any {
    return (async (...args: any[]) => ({ stdout: json, stderr: '' })) as any
  }

  function execThrow(status: number, stderr: string): any {
    return (async (...args: any[]) => {
      const e: any = new Error()
      e.status = status
      e.stderr = Buffer.from(stderr)
      throw e
    }) as any
  }

  it('clear scan returns clear', async () => {
    assert.equal((await scanVenvBlockers('/r', execReturn(okJson), stubVenv)).kind, 'clear')
  })

  it('blocked scan returns blocked', async () => {
    assert.equal((await scanVenvBlockers('/r', execReturn(blockedJson), stubVenv)).kind, 'blocked')
  })

  it('non-zero exit is probe-failure', async () => {
    const o = await scanVenvBlockers('/r', execThrow(2, 'ModuleNotFoundError'), stubVenv)
    assert.equal(o.kind, 'probe-failure')
  })

  it('missing venv python is probe-failure', async () => {
    const o = await scanVenvBlockers('/r', execReturn(okJson), () => null)
    assert.equal(o.kind, 'probe-failure')
  })

  it('malformed subprocess output is probe-failure', async () => {
    const o = await scanVenvBlockers('/r', execReturn('bad json'), stubVenv)
    assert.equal(o.kind, 'probe-failure')
  })

  it('calls subprocess with correct args, cwd and timeout', async () => {
    const calls: any[] = []

    const spy = (async (cmd: string, args: string[], opts: any) => {
      calls.push({ cmd, args, cwd: opts.cwd, timeout: opts.timeout })

      return { stdout: okJson, stderr: '' }
    }) as any

    await scanVenvBlockers('/update/root', spy, stubVenv)
    assert.equal(calls.length, 1)
    const c = calls[0]
    assert.ok(c.cmd.endsWith('python.exe'))
    assert.deepEqual(c.args, ['-m', 'hermes_cli._scan_venv_blockers'])
    assert.equal(c.cwd, '/update/root')
    assert.equal(typeof c.timeout, 'number')
    assert.ok(c.timeout > 0)
  })
})

// ---------------------------------------------------------------------------
// reapVenvOrphans — orphan reaper with injection
// ---------------------------------------------------------------------------

describe('reapVenvOrphans', () => {
  const clear: ScanOutcome = { kind: 'clear', result: { blocked: false, processes: [] } }
  const blocked1: ScanOutcome = {
    kind: 'blocked',
    result: { blocked: true, processes: [{ pid: 100, name: 'python.exe', cmdline: 'gateway' }] }
  }
  const blocked2: ScanOutcome = {
    kind: 'blocked',
    result: { blocked: true, processes: [{ pid: 200, name: 'bash.exe', cmdline: 'pty' }] }
  }

  function makeScan(seq: ScanOutcome[]) {
    let i = 0
    return () => {
      const v = seq[Math.min(i, seq.length - 1)]
      i++
      return Promise.resolve(v)
    }
  }

  function makeKill() {
    const killed: number[] = []
    const fn = (pid: number) => { killed.push(pid) }
    return { fn, killed }
  }

  function makeClock(start: number) {
    let t = start
    return {
      now: () => t,
      advance: (ms: number) => { t += ms }
    }
  }

  it('returns reaped=0 and clear when the first scan is clear', async () => {
    const scan = makeScan([clear])
    const kill = makeKill()
    const res = await reapVenvOrphans('/r', { scan: scan as any, kill: kill.fn })
    assert.equal(res.reaped, 0)
    assert.equal(res.final.kind, 'clear')
    assert.deepEqual(kill.killed, [])
  })

  it('kills blocked PIDs and re-scans until clear', async () => {
    const scan = makeScan([blocked1, blocked2, clear])
    const kill = makeKill()
    const res = await reapVenvOrphans('/r', { scan: scan as any, kill: kill.fn })
    assert.equal(res.reaped, 2)
    assert.equal(res.final.kind, 'clear')
    assert.deepEqual(kill.killed, [100, 200])
  })

  it('stops reaping when the deadline expires', async () => {
    const scan = makeScan([blocked1, blocked1, blocked1, blocked1])
    const kill = makeKill()
    const clock = makeClock(0)
    // deadlineMs=2500, pollMs=1000 => at most ~2 passes fit before deadline
    const res = await reapVenvOrphans('/r', {
      scan: scan as any,
      kill: kill.fn,
      now: clock.now,
      sleep: async () => { clock.advance(1000) }
    }, { deadlineMs: 2500, pollMs: 1000 })
    assert.ok(res.reaped >= 2)
    assert.equal(res.final.kind, 'blocked')
    assert.ok(kill.killed.length >= 2)
  })

  it('treats a probe-failure as terminal (stops, does not kill)', async () => {
    const probeFail: ScanOutcome = { kind: 'probe-failure', error: 'boom' }
    const scan = makeScan([probeFail])
    const kill = makeKill()
    const res = await reapVenvOrphans('/r', { scan: scan as any, kill: kill.fn })
    assert.equal(res.reaped, 0)
    assert.equal(res.final.kind, 'probe-failure')
    assert.deepEqual(kill.killed, [])
  })
})
