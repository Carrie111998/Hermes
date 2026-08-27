import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFile, stat } from 'node:fs/promises'

// Mock fs/promises before importing the module under test
vi.mock('node:fs/promises', () => ({
  readFile: vi.fn(),
  stat: vi.fn()
}))

vi.mock('node:os', () => ({}))

import {
  detectLocalGatewayRunning,
  invalidateLocalGatewayCache,
  type GatewayLiveness
} from './local-gateway-detect'

const mockReadFile = readFile as vi.MockedFunction<typeof readFile>
const mockStat = stat as vi.MockedFunction<typeof stat>

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

  it('returns alive=true when gateway is running with valid PID', async () => {
    mockReadFile.mockResolvedValue(JSON.stringify({
      gateway_state: 'running',
      pid: process.pid, // Use our own PID as a known-live process
      start_time: '2026-01-01T00:00:00Z',
      updated_at: new Date().toISOString(),
      argv: ['python', '-m', 'hermes_cli.main', 'gateway', '--port', '8642']
    }))
    const result = await detectLocalGatewayRunning()
    expect(result.alive).toBe(true)
    expect(result.pid).toBe(process.pid)
    expect(result.port).toBe(8642)
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
