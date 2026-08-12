import { mkdtempSync, writeFileSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'

import { describe, expect, it } from 'vitest'

import {
  adapterPortFromWsUrl,
  buildOfficeEnv,
  buildOfficeSettings,
  gatewayWsUrl,
  isWindowsCommandScript,
  pickWindowsCommandCandidate,
  writeOfficeFileIfChanged
} from './claw3d'

describe('command resolution helpers', () => {
  it('recognises Windows script extensions', () => {
    expect(isWindowsCommandScript('npm.cmd')).toBe(true)
    expect(isWindowsCommandScript('npm.bat')).toBe(true)
    expect(isWindowsCommandScript('npm.exe')).toBe(false)
    expect(isWindowsCommandScript('npm')).toBe(false)
  })

  it('prefers .exe over scripts and raw names', () => {
    const resolved = pickWindowsCommandCandidate(['npm', 'npm.cmd', 'C:\\node\\npm.exe'])
    expect(resolved).toEqual({ command: 'C:\\node\\npm.exe', windowsScript: false })
  })

  it('falls back to a script when no exe is present', () => {
    const resolved = pickWindowsCommandCandidate(['npm', 'npm.cmd'])
    expect(resolved).toEqual({ command: 'npm.cmd', windowsScript: true })
  })

  it('returns null for an empty candidate list', () => {
    expect(pickWindowsCommandCandidate([])).toBeNull()
  })
})

describe('gatewayWsUrl', () => {
  it('builds a ws URL with the token', () => {
    expect(gatewayWsUrl('http://127.0.0.1:8642', 'sekret')).toBe('ws://127.0.0.1:8642/api/ws?token=sekret')
  })

  it('upgrades https to wss', () => {
    expect(gatewayWsUrl('https://hermes.example.com', 'tok')).toBe('wss://hermes.example.com/api/ws?token=tok')
  })

  it('preserves a path prefix', () => {
    expect(gatewayWsUrl('http://localhost:8642/hermes', 't')).toBe('ws://localhost:8642/hermes/api/ws?token=t')
  })

  it('falls back to the raw input on parse failure', () => {
    expect(gatewayWsUrl('not a url', 't')).toBe('not a url')
  })
})

describe('adapterPortFromWsUrl', () => {
  it('derives the adapter port from the gateway port', () => {
    expect(adapterPortFromWsUrl('ws://127.0.0.1:18968/api/ws?token=x')).toBe(18989)
  })

  it('falls back to the default when no port is present', () => {
    expect(adapterPortFromWsUrl('ws://host/api/ws?token=x')).toBe(18989)
  })
})

describe('buildOfficeEnv', () => {
  it('writes the expected dotenv lines', () => {
    const env = buildOfficeEnv({
      port: 3000,
      url: 'ws://127.0.0.1:18968/api/ws?token=x',
      apiUrl: 'http://127.0.0.1:8642',
      apiKey: 'x',
      model: 'hermes',
      adapterPort: 18989
    })

    expect(env).toContain('PORT=3000')
    expect(env).toContain('HERMES_API_URL=http://127.0.0.1:8642')
    expect(env).toContain('HERMES_API_KEY=x')
    expect(env).toContain('HERMES_ADAPTER_PORT=18989')
    expect(env).toContain('CLAW3D_GATEWAY_ADAPTER_TYPE=hermes')
  })
})

describe('buildOfficeSettings', () => {
  it('merges gateway config while preserving existing fields', () => {
    const settings = buildOfficeSettings({ theme: 'dark' }, { url: 'http://127.0.0.1:8642', apiKey: 'k' })
    const gateway = settings.gateway as Record<string, unknown>
    const profiles = gateway.profiles as Record<string, { url: string; token: string }>
    const lastKnownGood = gateway.lastKnownGood as { adapterType: string }
    expect(settings.theme).toBe('dark')
    expect(settings.adapter).toBe('hermes')
    expect(gateway.url).toBe('http://127.0.0.1:8642')
    expect(profiles.hermes).toEqual({ url: 'http://127.0.0.1:8642', token: 'k' })
    expect(lastKnownGood.adapterType).toBe('hermes')
  })

  it('does not clobber an existing hermes profile', () => {
    const existing = {
      gateway: { profiles: { hermes: { url: 'http://old', token: 'old' }, other: { url: 'http://o', token: 't' } } }
    }

    const settings = buildOfficeSettings(existing, { url: 'http://new', apiKey: 'n' })
    const profiles = (settings.gateway as Record<string, unknown>).profiles as Record<string, { url: string; token: string }>
    expect(profiles.hermes).toEqual({ url: 'http://new', token: 'n' })
    expect(profiles.other).toEqual({ url: 'http://o', token: 't' })
  })
})

describe('writeOfficeFileIfChanged', () => {
  it('writes when missing and skips identical content', () => {
    const dir = mkdtempSync(join(tmpdir(), 'claw3d-test-'))
    const file = join(dir, 'nested', 'settings.json')
    expect(writeOfficeFileIfChanged(file, 'one')).toBe(true)
    expect(writeOfficeFileIfChanged(file, 'one')).toBe(false)
    writeFileSync(file, 'two')
    expect(writeOfficeFileIfChanged(file, 'one')).toBe(true)
  })
})
