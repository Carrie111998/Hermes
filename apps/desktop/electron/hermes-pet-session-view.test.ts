import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, describe, expect, it } from 'vitest'

import {
  HERMES_PET_SESSION_VIEW_MAX_RECENT,
  petSessionViewFocusGuard,
  readHermesPetSessionViewSnapshot,
  recordHermesPetSessionView
} from './hermes-pet-session-view'

const temporaryDirectories: string[] = []

function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-pet-view-'))
  temporaryDirectories.push(directory)

  return {
    directory,
    target: path.join(directory, 'hermes-pet-session-views-v1.json')
  }
}

afterEach(() => {
  while (temporaryDirectories.length) {
    fs.rmSync(temporaryDirectories.pop()!, { force: true, recursive: true })
  }
})

describe('Hermes Pet session view marker', () => {
  it('writes only bounded identity/profile/time evidence atomically', () => {
    const value = fixture()

    for (let index = 0; index < HERMES_PET_SESSION_VIEW_MAX_RECENT + 2; index += 1) {
      expect(
        recordHermesPetSessionView(
          value.target,
          {
            sessionID: `session-${index}`,
            profile: index % 2 === 0 ? 'default' : 'automation'
          },
          { now: 100 + index, producerPID: 42 }
        )
      ).toEqual({ ok: true })
    }

    const snapshot = readHermesPetSessionViewSnapshot(value.target)
    expect(snapshot?.schemaVersion).toBe(1)
    expect(snapshot?.producerPID).toBe(42)
    expect(snapshot?.recentViews).toHaveLength(HERMES_PET_SESSION_VIEW_MAX_RECENT)
    expect(snapshot?.current?.sessionID).toBe(`session-${HERMES_PET_SESSION_VIEW_MAX_RECENT + 1}`)
    expect(fs.readdirSync(value.directory).filter(entry => entry.includes('.tmp-'))).toEqual([])
    expect(fs.readFileSync(value.target, 'utf8')).not.toMatch(/prompt|command|credential/)
  })

  it('replaces a revisit and clears current without dropping recent history', () => {
    const value = fixture()

    recordHermesPetSessionView(value.target, { sessionID: 'same', profile: '' }, { now: 10, producerPID: 9 })
    recordHermesPetSessionView(value.target, { sessionID: 'same', profile: 'default' }, { now: 20, producerPID: 9 })
    recordHermesPetSessionView(value.target, { sessionID: null }, { now: 21, producerPID: 9 })

    expect(readHermesPetSessionViewSnapshot(value.target)).toMatchObject({
      current: null,
      recentViews: [{ sessionID: 'same', profile: 'default', viewedAt: 20 }]
    })
  })

  it('rejects unsafe input and unsupported/corrupt snapshots', () => {
    const value = fixture()

    expect(
      recordHermesPetSessionView(
        value.target,
        { sessionID: '../unsafe', profile: 'default' },
        { now: 10, producerPID: 9 }
      )
    ).toEqual({ ok: false, error: 'invalid-session-view' })
    expect(fs.existsSync(value.target)).toBe(false)

    fs.writeFileSync(value.target, '{broken', 'utf8')
    expect(readHermesPetSessionViewSnapshot(value.target)).toBeNull()
    expect(
      recordHermesPetSessionView(value.target, { sessionID: 'safe', profile: 'default' }, { now: 11, producerPID: 9 })
    ).toEqual({ ok: true })

    fs.writeFileSync(
      value.target,
      JSON.stringify({ schemaVersion: 2, updatedAt: 12, producerPID: 9, current: null, recentViews: [] }),
      'utf8'
    )
    expect(readHermesPetSessionViewSnapshot(value.target)).toBeNull()
  })
})

describe('petSessionViewFocusGuard (Electron sender-window hardening)', () => {
  const liveFocusedWindow = { isDestroyed: () => false, isFocused: () => true }

  it('rejects a missing, destroyed, or unfocused sender window', () => {
    expect(petSessionViewFocusGuard(null)).toBe('not-focused')
    expect(petSessionViewFocusGuard(undefined)).toBe('not-focused')
    expect(petSessionViewFocusGuard({ isDestroyed: () => true, isFocused: () => true })).toBe('not-focused')
    expect(petSessionViewFocusGuard({ isDestroyed: () => false, isFocused: () => false })).toBe('not-focused')
  })

  it('accepts a live focused sender window', () => {
    expect(petSessionViewFocusGuard(liveFocusedWindow)).toBeNull()
  })
})
