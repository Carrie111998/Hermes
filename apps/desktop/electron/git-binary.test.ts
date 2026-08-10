import assert from 'node:assert/strict'

import { test } from 'vitest'

import { type GitBinaryOptions, resolveGitBinary } from './git-binary'

// Candidate paths the Hermes resolver actually knows about.
const PROGRAM_FILES_GIT = 'C:\\Program Files\\Git\\cmd\\git.exe' // has a space
const LOCALAPP_PORTABLE_GIT = 'C:\\Users\\test\\AppData\\Local\\hermes\\git\\cmd\\git.exe' // no space

function makeOpts(overrides: Partial<GitBinaryOptions> = {}): GitBinaryOptions {
  return {
    isWindows: true,
    env: {},
    fileExists: () => false,
    findOnPath: () => null,
    ...overrides
  }
}

test('prefers the space-free PortableGit under LOCALAPPDATA over Program Files git', () => {
  const opts = makeOpts({
    env: {
      LOCALAPPDATA: 'C:\\Users\\test\\AppData\\Local',
      ProgramFiles: 'C:\\Program Files'
    },
    fileExists: p => p === PROGRAM_FILES_GIT || p === LOCALAPP_PORTABLE_GIT
  })

  const result = resolveGitBinary(opts)

  assert.equal(result, LOCALAPP_PORTABLE_GIT, 'must prefer the space-free known-install git')
  assert.ok(!result.includes(' '), 'resolved git must not contain a space')
})

test('falls back to the space-containing git when it is the only existing candidate and git is not on PATH', () => {
  const opts = makeOpts({
    env: { ProgramFiles: 'C:\\Program Files' },
    fileExists: p => p === PROGRAM_FILES_GIT,
    findOnPath: () => null
  })

  // Never break a machine whose sole git is Git for Windows under Program Files.
  assert.equal(resolveGitBinary(opts), PROGRAM_FILES_GIT)
})

test('returns bare "git" when the only known-install git has spaces but git resolves on PATH', () => {
  // This is the real-world case: git lives only under "Program Files" (space) but
  // `git` is discoverable on PATH. Returning the bare name keeps simple-git quiet
  // while still spawning a working git from the process PATH.
  const opts = makeOpts({
    env: { ProgramFiles: 'C:\\Program Files' },
    fileExists: p => p === PROGRAM_FILES_GIT,
    findOnPath: () => 'C:\\Program Files\\Git\\cmd\\git.exe'
  })

  const result = resolveGitBinary(opts)

  assert.equal(result, 'git', 'must return the bare name to avoid the simple-git space warning')
  assert.ok(!result.includes(' '), 'bare name has no space')
})

test('prefers a space-free known-install git over falling back to bare "git"', () => {
  const opts = makeOpts({
    env: {
      LOCALAPPDATA: 'C:\\Users\\test\\AppData\\Local',
      ProgramFiles: 'C:\\Program Files'
    },
    fileExists: p => p === PROGRAM_FILES_GIT || p === LOCALAPP_PORTABLE_GIT,
    findOnPath: () => 'C:\\Program Files\\Git\\cmd\\git.exe'
  })

  // The explicit space-free path is better than the bare name (no PATH reliance).
  assert.equal(resolveGitBinary(opts), LOCALAPP_PORTABLE_GIT)
})

test('uses git on PATH when no known-install candidate exists on disk', () => {
  const opts = makeOpts({
    env: { ProgramFiles: 'C:\\Program Files' },
    fileExists: () => false,
    findOnPath: () => 'C:\\some\\other\\git.exe'
  })

  assert.equal(resolveGitBinary(opts), 'git')
})

test('returns bare "git" when nothing exists and PATH lookup fails', () => {
  const opts = makeOpts({
    env: { ProgramFiles: 'C:\\Program Files' },
    fileExists: () => false,
    findOnPath: () => null
  })

  assert.equal(resolveGitBinary(opts), 'git')
})

test('non-Windows resolves via findOnPath only', () => {
  const opts = makeOpts({ isWindows: false, findOnPath: () => '/usr/bin/git' })

  assert.equal(resolveGitBinary(opts), '/usr/bin/git')
})

test('non-Windows returns bare "git" when findOnPath yields nothing', () => {
  const opts = makeOpts({ isWindows: false, findOnPath: () => null })

  assert.equal(resolveGitBinary(opts), 'git')
})
