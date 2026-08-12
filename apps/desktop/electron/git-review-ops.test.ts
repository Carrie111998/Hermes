import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import { gitFor, repoStatus, resolveRenamePath, reviewPush } from './git-review-ops'

const tempDirs: string[] = []

afterEach(() => {
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

// --- push remote resolution -------------------------------------------------
// In a fork checkout `origin` is frequently the UPSTREAM project rather than the
// user's own repo (~/.hermes/agent-src is exactly that). Pushing to a hardcoded
// `origin` therefore publishes private work to a public upstream and pins the
// branch's tracking there. These use real bare repos so each asserts *which*
// remote received the branch.

function makeBare(name: string) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-desktop-git-${name}-`))

  tempDirs.push(dir)
  execFileSync('git', ['init', '-q', '--bare'], { cwd: dir })

  return dir
}

function makeBranchRepo() {
  const dir = makeRepo()

  execFileSync('git', ['checkout', '-q', '-b', 'feature/x'], { cwd: dir })

  return dir
}

function hasBranch(bare: string, branch: string) {
  return execFileSync('git', ['branch', '--list', branch], { cwd: bare, encoding: 'utf8' }).includes(
    branch
  )
}

test('reviewPush refuses to guess origin when several remotes and no push default', async () => {
  const upstream = makeBare('upstream')
  const fork = makeBare('fork')
  const dir = makeBranchRepo()

  execFileSync('git', ['remote', 'add', 'origin', upstream], { cwd: dir })
  execFileSync('git', ['remote', 'add', 'fork', fork], { cwd: dir })

  await assert.rejects(() => reviewPush(dir, 'git'))
  assert.equal(hasBranch(upstream, 'feature/x'), false, 'published to the upstream remote')
  assert.equal(hasBranch(fork, 'feature/x'), false)
}, 120_000)

test('reviewPush uses the sole remote even when it is not named origin', async () => {
  const fork = makeBare('fork')
  const dir = makeBranchRepo()

  execFileSync('git', ['remote', 'add', 'daragao3', fork], { cwd: dir })

  await reviewPush(dir, 'git')

  assert.equal(hasBranch(fork, 'feature/x'), true)
}, 120_000)

test('reviewPush honours branch pushRemote over origin', async () => {
  const upstream = makeBare('upstream')
  const fork = makeBare('fork')
  const dir = makeBranchRepo()

  execFileSync('git', ['remote', 'add', 'origin', upstream], { cwd: dir })
  execFileSync('git', ['remote', 'add', 'fork', fork], { cwd: dir })
  execFileSync('git', ['config', 'branch.feature/x.pushRemote', 'fork'], { cwd: dir })

  await reviewPush(dir, 'git')

  assert.equal(hasBranch(fork, 'feature/x'), true)
  assert.equal(hasBranch(upstream, 'feature/x'), false)
}, 120_000)

test('reviewPush honours remote.pushDefault over origin', async () => {
  const upstream = makeBare('upstream')
  const fork = makeBare('fork')
  const dir = makeBranchRepo()

  execFileSync('git', ['remote', 'add', 'origin', upstream], { cwd: dir })
  execFileSync('git', ['remote', 'add', 'fork', fork], { cwd: dir })
  execFileSync('git', ['config', 'remote.pushDefault', 'fork'], { cwd: dir })

  await reviewPush(dir, 'git')

  assert.equal(hasBranch(fork, 'feature/x'), true)
  assert.equal(hasBranch(upstream, 'feature/x'), false)
}, 120_000)

test('reviewPush with an upstream still pushes to that upstream', async () => {
  const fork = makeBare('fork')
  const dir = makeBranchRepo()

  execFileSync('git', ['remote', 'add', 'fork', fork], { cwd: dir })
  execFileSync('git', ['push', '-q', '-u', 'fork', 'feature/x'], { cwd: dir })
  fs.writeFileSync(path.join(dir, 'tracked.txt'), 'changed\n')
  execFileSync('git', ['commit', '-qam', 'second'], { cwd: dir })

  await reviewPush(dir, 'git')

  assert.equal(
    execFileSync('git', ['rev-parse', 'feature/x'], { cwd: fork, encoding: 'utf8' }).trim(),
    execFileSync('git', ['rev-parse', 'HEAD'], { cwd: dir, encoding: 'utf8' }).trim()
  )
}, 120_000)
