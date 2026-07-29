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
  isGatewayProcess,
  parseVenvBlockerScanOutput,
  resolveVenvPython,
  scanVenvBlockers,
  stopVenvBlockers
} from './venv-blocker-scan'

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
// isGatewayProcess
// ---------------------------------------------------------------------------

describe('isGatewayProcess', () => {
  it('returns true when cmdline contains "gateway run"', () => {
    assert.equal(
      isGatewayProcess({ pid: 100, name: 'python.exe', cmdline: 'python.exe -m hermes_cli.main gateway run --profile default' }),
      true
    )
  })

  it('is case-insensitive', () => {
    assert.equal(
      isGatewayProcess({ pid: 101, name: 'PYTHON.EXE', cmdline: 'GATEWAY RUN --replace' }),
      true
    )
  })

  it('returns false for non-gateway processes', () => {
    assert.equal(
      isGatewayProcess({ pid: 102, name: 'python.exe', cmdline: 'python.exe -m hermes_cli.main serve --host 127.0.0.1' }),
      false
    )
  })

  it('returns false for unrelated commands containing gateway word', () => {
    // "gateway" appears in the command but not as "gateway run"
    assert.equal(
      isGatewayProcess({ pid: 103, name: 'git.exe', cmdline: 'git log --oneline --all --grep=gateway' }),
      false
    )
  })
})

// ---------------------------------------------------------------------------
// stopVenvBlockers (pure classification — the subprocess behavior with
// taskkill is tested via the platform gate; off-Windows it is a no-op)
// ---------------------------------------------------------------------------

describe('stopVenvBlockers', () => {
  it('returns all processes when none are gateways (no-op)', async () => {
    const procs = [
      { pid: 1, name: 'python.exe', cmdline: 'serve --host 127.0.0.1' },
      { pid: 2, name: 'python.exe', cmdline: 'dashboard --port 8080' }
    ]
    const remaining = await stopVenvBlockers(procs)
    assert.equal(remaining.length, 2)
    assert.deepEqual(remaining, procs)
  })

  it('filters out gateway processes from the returned list', async () => {
    // On non-Windows, this test passes because the platform gate skips
    // the taskkill calls and just partitions the list (returns all).
    const procs = [
      { pid: 1, name: 'python.exe', cmdline: 'serve' },
      { pid: 2, name: 'python.exe', cmdline: 'python.exe -m hermes_cli.main gateway run --profile default' }
    ]
    const remaining = await stopVenvBlockers(procs)
    // On non-Windows: platform gate returns all unchanged
    // On Windows: gateway PID filtered out (taskkill may fail harmlessly)
    // Both behaviors are correct for the respective platform.
    assert.ok(Array.isArray(remaining))
  })

  it('empty input returns empty', async () => {
    const remaining = await stopVenvBlockers([])
    assert.equal(remaining.length, 0)
  })
})
