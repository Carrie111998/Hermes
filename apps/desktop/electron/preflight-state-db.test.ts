import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import { listStateDbHomes, preflightStateDb } from './preflight-state-db'

function sqliteFile(dir: string): string {
  const buf = Buffer.alloc(128, 0)

  Buffer.from('SQLite format 3\0').copy(buf)
  fs.mkdirSync(dir, { recursive: true })
  const target = path.join(dir, 'state.db')

  fs.writeFileSync(target, buf)

  return target
}

function emergencyBackups(dir: string): string[] {
  return fs
    .readdirSync(dir)
    .filter(name => name.startsWith('state.db.pre-update-emergency-') && name.endsWith('.bak'))
    .sort()
}

describe('preflightStateDb (#97994)', () => {
  const roots: string[] = []

  afterEach(() => {
    for (const root of roots.splice(0)) {
      fs.rmSync(root, { force: true, recursive: true })
    }
  })

  it('lists the root home and every profiles/*/ directory', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-preflight-'))

    roots.push(home)
    fs.mkdirSync(path.join(home, 'profiles', 'paula'), { recursive: true })
    fs.mkdirSync(path.join(home, 'profiles', 'wesley'), { recursive: true })
    fs.writeFileSync(path.join(home, 'profiles', 'not-a-dir'), 'x')

    const homes = listStateDbHomes(home)

    expect(homes).toEqual([
      home,
      path.join(home, 'profiles', 'paula'),
      path.join(home, 'profiles', 'wesley')
    ])
  })

  it('snapshots profile databases, not only the root state.db', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-preflight-'))

    roots.push(home)
    sqliteFile(home)
    sqliteFile(path.join(home, 'profiles', 'paula'))

    const logs: string[] = []

    preflightStateDb(home, message => logs.push(message))

    expect(emergencyBackups(home)).toHaveLength(1)
    expect(emergencyBackups(path.join(home, 'profiles', 'paula'))).toHaveLength(1)
    expect(logs.some(line => line.includes('profiles') && line.includes('emergency'))).toBe(true)
  })

  it('skips a missing profile database without failing the root backup', () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-preflight-'))

    roots.push(home)
    sqliteFile(home)
    fs.mkdirSync(path.join(home, 'profiles', 'empty'), { recursive: true })

    const logs: string[] = []

    preflightStateDb(home, message => logs.push(message))

    expect(emergencyBackups(home)).toHaveLength(1)
    expect(emergencyBackups(path.join(home, 'profiles', 'empty'))).toHaveLength(0)
    expect(logs.some(line => line.includes('not found'))).toBe(true)
  })
})
