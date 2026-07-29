import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { execFileNoThrow } from './execFileNoThrow.js'

// These tests rely on detached child-process semantics and inherited stdio.
const onWindows = process.platform === 'win32'

let scriptDir: string
let daemonScript: string
let sleeperPids: number[]

function trackSleeperPid(pidFile: string): void {
  try {
    const pid = parseInt(readFileSync(pidFile, 'utf8').trim(), 10)

    if (pid > 0) {
      sleeperPids.push(pid)
    }
  } catch {
    // PID file not written or unreadable — sleeper may have already exited.
  }
}

beforeEach(() => {
  sleeperPids = []
  scriptDir = join(tmpdir(), `hermes-execfile-test-${process.pid}-${Date.now()}`)
  mkdirSync(scriptDir, { recursive: true })
  daemonScript = join(scriptDir, 'fake-daemonizer.cjs')
  writeFileSync(
    daemonScript,
    `const fs = require('node:fs');
const { spawn } = require('node:child_process');
const pidFile = process.argv[2];
const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 3000)'], {
  detached: true,
  stdio: 'inherit'
});
child.unref();
fs.writeFileSync(pidFile, String(child.pid));
process.exit(0);
`
  )
})

afterEach(() => {
  for (const pid of sleeperPids) {
    try {
      process.kill(pid, 'SIGKILL')
    } catch {
      // Already exited — fine.
    }
  }

  rmSync(scriptDir, { recursive: true, force: true })
})

describe.skipIf(onWindows)('execFileNoThrow with daemon-style children', () => {
  it("settles immediately on 'exit' when resolveOnExit is true, regardless of daemon stdio", async () => {
    const pidFile = join(scriptDir, 'sleeper-exit.pid')
    const start = Date.now()

    const result = await execFileNoThrow(process.execPath, [daemonScript, pidFile], {
      timeout: 2000,
      resolveOnExit: true
    })

    trackSleeperPid(pidFile)

    const elapsed = Date.now() - start

    expect(result.code).toBe(0)
    expect(elapsed).toBeLessThan(500)
  })

  it("still surfaces the right code when resolveOnExit'd child exits non-zero", async () => {
    const pidFile = join(scriptDir, 'sleeper-fail.pid')
    const failScript = join(scriptDir, 'fail.cjs')
    writeFileSync(
      failScript,
      `const fs = require('node:fs');
const { spawn } = require('node:child_process');
const pidFile = ${JSON.stringify(pidFile)};
const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 3000)'], {
  detached: true,
  stdio: 'inherit'
});
child.unref();
fs.writeFileSync(pidFile, String(child.pid));
process.exit(7);
`
    )

    const result = await execFileNoThrow(process.execPath, [failScript], {
      timeout: 2000,
      resolveOnExit: true
    })

    trackSleeperPid(pidFile)

    expect(result.code).toBe(7)
  })

  it('settles on timeout=124 when the child itself never exits, even with resolveOnExit', async () => {
    const slowScript = join(scriptDir, 'slow.cjs')
    writeFileSync(slowScript, 'setTimeout(() => {}, 30000)\n')

    const result = await execFileNoThrow(process.execPath, [slowScript], {
      timeout: 200,
      resolveOnExit: true
    })

    expect(result.code).toBe(124)
  })

  it('does not double-resolve when both timer and exit fire', async () => {
    const pidFile = join(scriptDir, 'sleeper-race.pid')

    const result = await execFileNoThrow(process.execPath, [daemonScript, pidFile], {
      timeout: 50,
      resolveOnExit: true
    })

    trackSleeperPid(pidFile)

    expect([0, 124]).toContain(result.code)
  })
})
