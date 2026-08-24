import { describe, expect, it } from 'vitest'

import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { allowedFsRoots, assertPathWithinRoots, assertRealPathWithinRoots, isPathWithinRoots } from './fs-scope'

describe('filesystem scope', () => {
  const roots = allowedFsRoots('/home/bode/.hermes', '/work/project')

  it('allows Hermes-owned and active-workspace paths', () => {
    expect(isPathWithinRoots('/home/bode/.hermes/logs/app.log', roots)).toBe(true)
    expect(isPathWithinRoots('/work/project/src/file.ts', roots)).toBe(true)
  })

  it('rejects arbitrary user-writable paths and traversal escapes', () => {
    expect(isPathWithinRoots('/tmp/other.txt', roots)).toBe(false)
    expect(isPathWithinRoots('/work/project/../secrets.txt', roots)).toBe(false)
    expect(() => assertPathWithinRoots('/tmp/other.txt', roots, 'Delete')).toThrow(
      'Delete is outside an allowed workspace root'
    )
  })

  it('rejects a symlink inside an allowed root that resolves outside it', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-scope-'))
    const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-outside-'))
    const link = path.join(root, 'link')

    try {
      fs.symlinkSync(outside, link, 'dir')
      expect(() => assertRealPathWithinRoots(path.join(link, 'secret.txt'), [root], 'Read')).toThrow(
        'Read does not permit symbolic links'
      )
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
      fs.rmSync(outside, { recursive: true, force: true })
    }
  })

  it('rejects a dangling symlink in an allowed root', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-scope-'))
    const link = path.join(root, 'dangling')

    try {
      fs.symlinkSync(path.join(root, 'missing-target'), link, 'dir')
      expect(() => assertRealPathWithinRoots(path.join(link, 'new.txt'), [root], 'Write')).toThrow()
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
  })

  it('accepts an in-root file URL after converting it to a filesystem path', () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-scope-'))
    const file = path.join(root, 'safe.txt')
    try {
      fs.writeFileSync(file, 'safe')
      expect(assertRealPathWithinRoots(`file://${file}`, [root], 'Read')).toBe(fs.realpathSync(file))
    } finally {
      fs.rmSync(root, { recursive: true, force: true })
    }
  })
})
