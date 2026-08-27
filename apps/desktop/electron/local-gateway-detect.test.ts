import { readFile } from 'node:fs/promises'

import { beforeEach, describe, expect, it, vi } from 'vitest'

// Mock fs/promises before importing the module under test
vi.mock('node:fs/promises', () => ({
  readFile: vi.fn()
}))

import {
  detectLocalGatewayRunning,
  invalidateLocalGatewayCache
} from './local-gateway-detect'

// vi.mocked() is the canonical typed cast for a module-mocked function.
const mockReadFile = vi.mocked(readFile)

/**
 * Builds a synthetic `/proc/<pid>/stat` line. comm (field 2) is parenthesized;
 * the module splits after the final ')' so its tail starts at field 3 (array
 * index 2), making field 22 (starttime) the tail's index 19 -> array index 21.
 * Fields 4..21 (array indices 3..20) are the 18 filler items before it.
 */
function statLine(field22: number): string {
  const fields: string[] = ['0', '(sleep)', 'S']
  for (let i = 3; i <= 20; i++) {
    fields.push('0') // fields 4..21
  }
  fields.push(String(field22)) // field 22 = starttime (clock ticks)
  return fields.join(' ')
}

describe('detectLocalGatewayRunning', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    invalidateLocalGatewayCache()
    delete process.env.HERMES_HOME
    process.env.HOME = '/tmp/test-home'
  })

  it('returns no-hermes-home when HOME and HERMES_HOME are both unset', async () => {
    delete process.env.HOME
    delete process.env.USERPROFILE
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(false)
    expect(result.reason).toBe('no-hermes-home')
  })

  it('returns no-state when gateway_state.json does not exist', async () => {
    mockReadFile.mockRejectedValue(new Error('ENOENT'))
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(false)
    expect(result.reason).toBe('no-state')
  })

  it('returns state-not-running when gateway_state is starting', async () => {
    mockReadFile.mockResolvedValue(JSON.stringify({
      gateway_state: 'starting',
      pid: 1234,
      updated_at: new Date().toISOString()
    }))
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(false)
    expect(result.reason).toBe('state-not-running')
  })

  it('returns state-not-running when gateway_state is stopped', async () => {
    mockReadFile.mockResolvedValue(JSON.stringify({
      gateway_state: 'stopped',
      pid: 1234,
      updated_at: new Date().toISOString()
    }))
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(false)
    expect(result.reason).toBe('state-not-running')
  })

  it('returns pid-missing when pid is null', async () => {
    mockReadFile.mockResolvedValue(JSON.stringify({
      gateway_state: 'running',
      pid: null,
      updated_at: new Date().toISOString()
    }))
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(false)
    expect(result.reason).toBe('pid-missing')
  })

  it('returns state-stale when PID is gone and the record is stale (ungraceful kill)', async () => {
    // #91564 regression guard: liveness keys off the PID alone; `updated_at`
    // only classifies a DEAD pid. A dead pid + a record past the TTL is the
    // signature of an ungraceful kill (shutdown handler never ran).
    const stale = new Date(Date.now() - 180_000).toISOString()
    const deadPid = 9_999_999 // well above pid_max — guaranteed ESRCH from kill(2)
    mockReadFile.mockResolvedValue(JSON.stringify({
      gateway_state: 'running',
      pid: deadPid,
      updated_at: stale
    }))
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(false)
    expect(result.reason).toBe('state-stale')
    expect(result.pid).toBe(deadPid)
  })

  it('returns pid-dead when PID is gone but the record is fresh', async () => {
    const deadPid = 9_999_999
    mockReadFile.mockResolvedValue(JSON.stringify({
      gateway_state: 'running',
      pid: deadPid,
      updated_at: new Date().toISOString()
    }))
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(false)
    expect(result.reason).toBe('pid-dead')
    expect(result.pid).toBe(deadPid)
  })

  it('adopts a live gateway even when updated_at is stale (idle gateway, #91564)', async () => {
    // THIS is the regression the moved guard exists to fix: a healthy *idle*
    // gateway never advances `updated_at` (only rewritten on state transitions
    // and platform events), so gating adoption on freshness wrongly declared
    // it dead and spawned the duplicate serve. A live PID must win.
    const stale = new Date(Date.now() - 30 * 60_000).toISOString()
    mockReadFile.mockResolvedValue(JSON.stringify({
      gateway_state: 'running',
      pid: process.pid, // our own (known-live) PID
      updated_at: stale,
      argv: ['python', '-m', 'hermes_cli.main', 'gateway', '--port', '9011']
    }))
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(true)
    expect(result.pid).toBe(process.pid)
    expect(result.port).toBe(9011)
    expect(result.reason).toBe('state-running')
  })

  it('adopts a live gateway whose start_time fingerprint matches (pid-reuse guard)', async () => {
    // The fingerprint guard: recorded start_time (clock ticks, field 22) must
    // equal the live process's field 22. A match => same process => alive.
    const field22 = 4_567_890
    mockReadFile.mockImplementation(async (path) => {
      const file = String(path)
      if (file.startsWith('/proc/')) {
          return statLine(field22)
        }
      return JSON.stringify({
        gateway_state: 'running',
        pid: process.pid, // our own (known-live) PID
        start_time: field22,
        updated_at: new Date().toISOString(),
        argv: ['python', '-m', 'hermes_cli.main', 'gateway', '--port', '8642']
      })
    })
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(true)
    expect(result.pid).toBe(process.pid)
    expect(result.port).toBe(8642)
    expect(result.reason).toBe('state-running')
  })

  it('rejects a live PID whose start_time fingerprint differs (pid-reused)', async () => {
    // Same PID number, different process: the recorded gateway is gone and the
    // PID was recycled. Field 22 differs from the recorded start_time.
    mockReadFile.mockImplementation(async (path) => {
      const file = String(path)
      if (file.startsWith('/proc/')) {
          return statLine(9_999_999) // live value differs
        }
      return JSON.stringify({
        gateway_state: 'running',
        pid: process.pid, // known-live PID number...
        start_time: 4_567_890, // ...but a different process than recorded
        updated_at: new Date().toISOString()
      })
    })
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(false)
    expect(result.reason).toBe('pid-reused')
    expect(result.pid).toBe(process.pid)
  })

  it('abstains (adopts) when the /proc fingerprint is unreadable', async () => {
    // No /proc (Windows/macOS) or unreadable stat => null fingerprint => the
    // guard abstains and a live PID is accepted — never a false pid-reused.
    mockReadFile.mockImplementation(async (path) => {
      const file = String(path)
      if (file.startsWith('/proc/')) {
          throw new Error('ENOENT')
        }
      return JSON.stringify({
        gateway_state: 'running',
        pid: process.pid,
        start_time: 4_567_890, // recorded, but the live side cannot be read
        updated_at: new Date().toISOString()
      })
    })
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(true)
    expect(result.reason).toBe('state-running')
  })

  it('extracts --port=N form from argv', async () => {
    mockReadFile.mockResolvedValue(JSON.stringify({
      gateway_state: 'running',
      pid: process.pid,
      updated_at: new Date().toISOString(),
      argv: ['python', '-m', 'hermes_cli.main', 'gateway', '--port=9000']
    }))
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(true)
    expect(result.port).toBe(9000)
  })

  it('returns port=null when argv has no --port', async () => {
    mockReadFile.mockResolvedValue(JSON.stringify({
      gateway_state: 'running',
      pid: process.pid,
      updated_at: new Date().toISOString(),
      argv: ['python', '-m', 'hermes_cli.main', 'gateway']
    }))
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(true)
    expect(result.port).toBeNull()
  })
})
