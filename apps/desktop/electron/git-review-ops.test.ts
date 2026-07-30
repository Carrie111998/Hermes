import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import simpleGit from 'simple-git'
import { afterEach, test, vi } from 'vitest'

import { gitFor, repoStatus, resolveRenamePath, reviewRevert } from './git-review-ops'

vi.mock('simple-git', { spy: true })

const tempDirs: string[] = []

afterEach(() => {
  vi.clearAllMocks()

  for (const dir of tempDirs.splice(0)) {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

function makeRepo() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-git-status-'))

  tempDirs.push(dir)
  execFileSync('git', ['init', '-q'], { cwd: dir })
  execFileSync('git', ['config', 'user.email', 'hermes-test@example.com'], { cwd: dir })
  execFileSync('git', ['config', 'user.name', 'Hermes Test'], { cwd: dir })
  fs.writeFileSync(path.join(dir, 'tracked.txt'), 'tracked\n')
  execFileSync('git', ['add', 'tracked.txt'], { cwd: dir })
  execFileSync('git', ['commit', '-qm', 'initial'], { cwd: dir })

  return dir
}

// A repo with the two shapes the review pane always has to handle at once: a
// tracked edit and a brand-new file.
function makeDirtyRepo() {
  const dir = makeRepo()

  fs.writeFileSync(path.join(dir, 'tracked.txt'), 'tracked\nedited\n')
  fs.writeFileSync(path.join(dir, 'new.txt'), 'brand new\n')

  return dir
}

function makeTempDir() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-git-plain-'))

  tempDirs.push(dir)

  return dir
}

async function changedPaths(dir: string) {
  return (await repoStatus(dir, 'git'))?.files.map(file => file.path)
}

test('resolveRenamePath: plain path is unchanged', () => {
  assert.equal(resolveRenamePath('src/a.ts'), 'src/a.ts')
})

test('gitFor accepts an internally resolved git binary path containing spaces', () => {
  assert.doesNotThrow(() => gitFor(process.cwd(), 'C:\\Program Files\\Git\\cmd\\git.exe'))
})

test('gitFor runs git through a spaced binary path', async () => {
  if (process.platform !== 'win32') {
    return
  }

  const gitBin = path.join(process.env.ProgramFiles || String.raw`C:\Program Files`, 'Git', 'cmd', 'git.exe')

  if (!fs.existsSync(gitBin)) {
    return
  }

  const repo = makeRepo()

  fs.writeFileSync(path.join(repo, 'changed.txt'), 'review me\n')

  const status = await gitFor(repo, gitBin).status()

  assert.equal(status.not_added.includes('changed.txt'), true)
})

test('resolveRenamePath: simple rename resolves to the new path', () => {
  assert.equal(resolveRenamePath('old.ts => new.ts'), 'new.ts')
})

test('resolveRenamePath: brace rename resolves to the new path', () => {
  assert.equal(resolveRenamePath('src/{old => new}/file.ts'), 'src/new/file.ts')
})

test('resolveRenamePath: brace rename collapsing a segment', () => {
  assert.equal(resolveRenamePath('src/{lib => }/file.ts'), 'src/file.ts')
})

test('repoStatus reports an untracked directory without recursively listing its contents', async () => {
  const dir = makeRepo()
  const nested = path.join(dir, 'generated', 'deep')

  fs.mkdirSync(nested, { recursive: true })
  fs.writeFileSync(path.join(nested, 'large-output.txt'), 'generated\n')

  const status = await repoStatus(dir, 'git')

  assert.ok(status)
  assert.equal(status.untracked, 1)
  assert.equal(status.changed, 1)
  assert.deepEqual(
    status.files.map(file => file.path),
    ['generated/']
  )
})

test('reviewRevert removes a staged new file', async () => {
  // Staging is one click away from reverting in the review pane, and a staged
  // new file is what `checkout HEAD` (not in HEAD) and `clean` (tracked in the
  // index) both refuse to touch.
  const dir = makeDirtyRepo()

  execFileSync('git', ['add', 'new.txt'], { cwd: dir })

  assert.deepEqual(await reviewRevert(dir, 'new.txt', 'git'), { ok: true })
  assert.equal(fs.existsSync(path.join(dir, 'new.txt')), false)
  // Scoped: the unrelated tracked edit is left alone.
  assert.deepEqual(await changedPaths(dir), ['tracked.txt'])
})

test('reviewRevert removes a plain untracked file', async () => {
  // `checkout HEAD` legitimately fails here (the path is not in HEAD); `clean`
  // is the whole job, so that failure must not abort the revert.
  const dir = makeDirtyRepo()

  assert.deepEqual(await reviewRevert(dir, 'new.txt', 'git'), { ok: true })
  assert.equal(fs.existsSync(path.join(dir, 'new.txt')), false)
  assert.deepEqual(await changedPaths(dir), ['tracked.txt'])
})

test('reviewRevert restores a staged modification', async () => {
  const dir = makeDirtyRepo()

  execFileSync('git', ['add', 'tracked.txt'], { cwd: dir })

  assert.deepEqual(await reviewRevert(dir, 'tracked.txt', 'git'), { ok: true })
  assert.equal(fs.readFileSync(path.join(dir, 'tracked.txt'), 'utf8'), 'tracked\n')
  assert.deepEqual(await changedPaths(dir), ['new.txt'])
})

test('reviewRevert restores a staged deletion', async () => {
  const dir = makeDirtyRepo()

  execFileSync('git', ['rm', '-q', '-f', 'tracked.txt'], { cwd: dir })

  assert.deepEqual(await reviewRevert(dir, 'tracked.txt', 'git'), { ok: true })
  assert.equal(fs.readFileSync(path.join(dir, 'tracked.txt'), 'utf8'), 'tracked\n')
  assert.deepEqual(await changedPaths(dir), ['new.txt'])
})

test('reviewRevert with no path clears staged, unstaged and untracked changes', async () => {
  const dir = makeDirtyRepo()

  fs.writeFileSync(path.join(dir, 'staged-new.txt'), 'staged\n')
  execFileSync('git', ['add', 'staged-new.txt'], { cwd: dir })

  assert.deepEqual(await reviewRevert(dir, null, 'git'), { ok: true })
  assert.equal(fs.readFileSync(path.join(dir, 'tracked.txt'), 'utf8'), 'tracked\n')
  assert.equal(fs.existsSync(path.join(dir, 'new.txt')), false)
  assert.equal(fs.existsSync(path.join(dir, 'staged-new.txt')), false)
  assert.deepEqual(await changedPaths(dir), [])
})

test('reviewRevert removes new files before the first commit', async () => {
  // An unborn HEAD has nothing to restore, but the new files still have to go.
  const dir = makeTempDir()

  execFileSync('git', ['init', '-q'], { cwd: dir })
  fs.writeFileSync(path.join(dir, 'staged.txt'), 'staged\n')
  fs.writeFileSync(path.join(dir, 'loose.txt'), 'loose\n')
  execFileSync('git', ['add', 'staged.txt'], { cwd: dir })

  assert.deepEqual(await reviewRevert(dir, null, 'git'), { ok: true })
  assert.equal(fs.existsSync(path.join(dir, 'staged.txt')), false)
  assert.equal(fs.existsSync(path.join(dir, 'loose.txt')), false)
})

test('reviewRevert all does not require HEAD during reset', async () => {
  const dir = makeTempDir()

  const raw = vi.fn(async args => {
    if (args[0] === 'reset' && args.includes('HEAD')) {
      throw new Error("fatal: ambiguous argument 'HEAD'")
    }

    return ''
  })

  vi.mocked(simpleGit).mockReturnValueOnce({ raw } as any)

  assert.deepEqual(await reviewRevert(dir, null, 'git'), { ok: true })
  assert.deepEqual(
    raw.mock.calls.map(([args]) => args),
    [
      ['reset', '-q'],
      ['rev-parse', '--verify', '--quiet', 'HEAD'],
      ['clean', '-fd', '--', '.']
    ]
  )
})

test('reviewRevert surfaces ls-tree failures when HEAD exists', async () => {
  const dir = makeTempDir()

  const raw = vi.fn(async args => {
    if (args[0] === 'rev-parse') {
      return 'deadbeef\n'
    }

    if (args[0] === 'ls-tree') {
      throw new Error('injected ls-tree failure')
    }

    return ''
  })

  vi.mocked(simpleGit).mockReturnValueOnce({ raw } as any)

  await assert.rejects(() => reviewRevert(dir, 'tracked.txt', 'git'), /injected ls-tree failure/)
})

test('reviewRevert rejects on a git failure instead of reporting success', async () => {
  const dir = makeTempDir()

  fs.writeFileSync(path.join(dir, 'note.txt'), 'keep me\n')

  await assert.rejects(() => reviewRevert(dir, 'note.txt', 'git'))
  assert.equal(fs.readFileSync(path.join(dir, 'note.txt'), 'utf8'), 'keep me\n')
})
