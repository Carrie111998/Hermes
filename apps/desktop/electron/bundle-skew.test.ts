import { execFileSync } from 'node:child_process'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterAll, describe, expect, it } from 'vitest'

import { detectBundleSkew, isFallbackCommit, type RunGit } from './bundle-skew'

const REPO = '/repo'
const STAMP = { commit: 'a'.repeat(40), source: 'ci' }

function gitReturning(stdout: string, code = 0): RunGit {
  return async () => ({ code, stderr: '', stdout })
}

describe('isFallbackCommit', () => {
  it('matches the all-zero placeholder at any stamp length', () => {
    expect(isFallbackCommit('0'.repeat(40))).toBe(true)
    expect(isFallbackCommit('0'.repeat(7))).toBe(true)
    expect(isFallbackCommit('a'.repeat(40))).toBe(false)
  })
})

describe('detectBundleSkew', () => {
  it('reports stale when desktop commits landed after the stamp', async () => {
    const result = await detectBundleSkew(STAMP, gitReturning('3\n'), REPO)

    expect(result).toEqual({ desktopCommitsBehind: 3, outOfSync: true })
  })

  it('counts only commits that touch runtime desktop paths', async () => {
    let seen: string[] = []

    const git: RunGit = async args => {
      seen = args

      return { code: 0, stderr: '', stdout: '0' }
    }

    await detectBundleSkew(STAMP, git, REPO)

    expect(seen).toEqual([
      'rev-list',
      '--count',
      `${STAMP.commit}..HEAD`,
      '--',
      'apps/desktop/src',
      'apps/desktop/electron',
      'apps/desktop/index.html',
      'apps/desktop/public',
      'apps/desktop/assets',
      'apps/desktop/package.json',
      'apps/desktop/vite.config.ts'
    ])
  })

  it('is quiet when no desktop commits follow the stamp', async () => {
    const result = await detectBundleSkew(STAMP, gitReturning('0\n'), REPO)

    expect(result).toEqual({ desktopCommitsBehind: 0, outOfSync: false })
  })

  it('is quiet without a stamp (dev runs)', async () => {
    expect(await detectBundleSkew(null, gitReturning('9'), REPO)).toEqual({
      desktopCommitsBehind: null,
      outOfSync: false
    })
  })

  it('is quiet on a fallback stamp (non-git build)', async () => {
    const fallback = { commit: '0'.repeat(40), source: 'fallback' }

    expect(await detectBundleSkew(fallback, gitReturning('9'), REPO)).toEqual({
      desktopCommitsBehind: null,
      outOfSync: false
    })
  })

  it('is quiet when git fails (unknown commit, shallow clone, no git)', async () => {
    expect(await detectBundleSkew(STAMP, gitReturning('', 128), REPO)).toEqual({
      desktopCommitsBehind: null,
      outOfSync: false
    })
  })

  it('is quiet when git throws', async () => {
    const git: RunGit = async () => {
      throw new Error('spawn ENOENT')
    }

    expect(await detectBundleSkew(STAMP, git, REPO)).toEqual({
      desktopCommitsBehind: null,
      outOfSync: false
    })
  })

  it('is quiet on unparsable rev-list output', async () => {
    const result = await detectBundleSkew(STAMP, gitReturning('fatal: bad object'), REPO)

    expect(result).toEqual({ desktopCommitsBehind: null, outOfSync: false })
  })
})

// Real-git integration: proves the pathspec discriminates docs/e2e-only
// commits from runtime commits in an actual repository, not just via mocks.
const scratchRepos: string[] = []

afterAll(() => {
  for (const dir of scratchRepos) {
    execFileSync('rm', ['-rf', dir])
  }
})

function makeScratchRepo(): { repoRoot: string; base: string } {
  const repoRoot = mkdtempSync(join(tmpdir(), 'bundle-skew-'))
  scratchRepos.push(repoRoot)

  const git = (...args: string[]) =>
    execFileSync('git', ['-c', 'user.email=skew@test', '-c', 'user.name=skew', ...args], {
      cwd: repoRoot,
      stdio: ['ignore', 'pipe', 'pipe']
    })

  git('init', '-q')
  git('commit', '-q', '--allow-empty', '-m', 'base')
  const base = git('rev-parse', 'HEAD').toString().trim()

  return { repoRoot, base }
}

function realGitRun(root: string): RunGit {
  return async (args, options) => {
    try {
      const stdout = execFileSync('git', args, {
        cwd: options.cwd || root,
        stdio: ['ignore', 'pipe', 'pipe']
      }).toString()

      return { code: 0, stderr: '', stdout }
    } catch (error) {
      const e = error as { stdout?: Buffer; stderr?: Buffer; status?: number }

      return {
        code: e.status ?? 1,
        stderr: e.stderr?.toString() ?? '',
        stdout: e.stdout?.toString() ?? ''
      }
    }
  }
}

describe('detectBundleSkew against a real git repo', () => {
  it('is quiet when only docs and e2e specs changed under apps/desktop', async () => {
    const { repoRoot, base } = makeScratchRepo()

    const git = (...args: string[]) =>
      execFileSync('git', ['-c', 'user.email=skew@test', '-c', 'user.name=skew', ...args], {
        cwd: repoRoot
      })

    execFileSync('mkdir', ['-p', join(repoRoot, 'apps/desktop/e2e')])
    execFileSync('touch', [join(repoRoot, 'apps/desktop/AGENTS.md')])
    execFileSync('touch', [join(repoRoot, 'apps/desktop/e2e/boot.spec.ts')])
    git('add', '.')
    git('commit', '-q', '-m', 'docs and e2e only')

    const result = await detectBundleSkew(
      { commit: base, source: 'local' },
      realGitRun(repoRoot),
      repoRoot
    )

    expect(result).toEqual({ desktopCommitsBehind: 0, outOfSync: false })
  })

  it('warns when a renderer file changed under apps/desktop', async () => {
    const { repoRoot, base } = makeScratchRepo()

    const git = (...args: string[]) =>
      execFileSync('git', ['-c', 'user.email=skew@test', '-c', 'user.name=skew', ...args], {
        cwd: repoRoot
      })

    execFileSync('mkdir', ['-p', join(repoRoot, 'apps/desktop/src/app')])
    execFileSync('touch', [join(repoRoot, 'apps/desktop/src/app/new-feature.tsx')])
    execFileSync('touch', [join(repoRoot, 'apps/desktop/README.md')])
    git('add', '.')
    git('commit', '-q', '-m', 'renderer change')

    const result = await detectBundleSkew(
      { commit: base, source: 'local' },
      realGitRun(repoRoot),
      repoRoot
    )

    expect(result).toEqual({ desktopCommitsBehind: 1, outOfSync: true })
  })
})
