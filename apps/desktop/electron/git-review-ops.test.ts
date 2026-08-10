import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import {
  gitFor,
  repoStatus,
  resolveRenamePath,
  REVIEW_FILE_CAP,
  reviewCommit,
  reviewCreatePushRequest,
  reviewList,
  reviewPushApproved
} from './git-review-ops'

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

function makeBareRemote() {
  const remote = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-desktop-git-remote-'))

  tempDirs.push(remote)
  execFileSync('git', ['init', '--bare', '-q'], { cwd: remote })

  return remote
}

function attachBareRemote(repo: string) {
  const remote = makeBareRemote()

  execFileSync('git', ['branch', '-M', 'main'], { cwd: repo })
  execFileSync('git', ['remote', 'add', 'origin', remote], { cwd: repo })
  execFileSync('git', ['push', '-qu', 'origin', 'main'], { cwd: repo })

  return remote
}

function commitChange(repo: string, content: string) {
  fs.writeFileSync(path.join(repo, 'tracked.txt'), content)
  execFileSync('git', ['add', 'tracked.txt'], { cwd: repo })
  execFileSync('git', ['commit', '-qm', content.trim()], { cwd: repo })
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

test('reviewList reports an untracked directory without recursively listing its contents', async () => {
  const dir = makeRepo()
  const nested = path.join(dir, 'browser-profile', 'Default', 'Cache')

  fs.mkdirSync(nested, { recursive: true })

  for (let i = 0; i < 20; i++) {
    fs.writeFileSync(path.join(nested, `cache-${i}.bin`), 'generated\n')
  }

  const result = await reviewList(dir, 'uncommitted', null, 'git')

  assert.deepEqual(
    result.files.map(file => file.path),
    ['browser-profile/']
  )
})

test('reviewList caps the file payload returned to the renderer', async () => {
  const dir = makeRepo()

  for (let i = 0; i < REVIEW_FILE_CAP + 10; i++) {
    fs.writeFileSync(path.join(dir, `untracked-${String(i).padStart(4, '0')}.txt`), 'generated\n')
  }

  const result = await reviewList(dir, 'uncommitted', null, 'git')

  assert.equal(result.files.length, REVIEW_FILE_CAP)
})

test('push approval is bound to the host-derived commit and is single use', async () => {
  const repo = makeRepo()
  const remote = attachBareRemote(repo)

  commitChange(repo, 'approved\n')
  const request = await reviewCreatePushRequest(repo, 'git')

  const decision = {
    ...request,
    approved: true,
    approvedBy: 'desktop-user',
    decidedAt: new Date().toISOString()
  }

  await reviewPushApproved(repo, decision, 'git')

  const localHead = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repo, encoding: 'utf8' }).trim()
  const remoteHead = execFileSync('git', ['rev-parse', 'refs/heads/main'], { cwd: remote, encoding: 'utf8' }).trim()

  assert.equal(remoteHead, localHead)
  await assert.rejects(() => reviewPushApproved(repo, decision, 'git'), /already used|unknown/i)
})

test('first approved push of a workspace branch establishes its upstream', async () => {
  const repo = makeRepo()
  attachBareRemote(repo)
  execFileSync('git', ['checkout', '-b', 'feature/workspace'], { cwd: repo })
  commitChange(repo, 'workspace\n')
  const request = await reviewCreatePushRequest(repo, 'git')
  await reviewPushApproved(
    repo,
    {
      ...request,
      approved: true,
      approvedBy: 'desktop-user',
      decidedAt: new Date().toISOString()
    },
    'git'
  )

  const upstream = execFileSync(
    'git',
    ['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}'],
    { cwd: repo, encoding: 'utf8' }
  ).trim()

  assert.equal(upstream, 'origin/feature/workspace')
})

test('push approval is invalidated when HEAD changes after the request', async () => {
  const repo = makeRepo()
  const remote = attachBareRemote(repo)
  const remoteBefore = execFileSync('git', ['rev-parse', 'refs/heads/main'], { cwd: remote, encoding: 'utf8' }).trim()

  commitChange(repo, 'first\n')
  const request = await reviewCreatePushRequest(repo, 'git')
  commitChange(repo, 'second\n')

  await assert.rejects(
    () =>
      reviewPushApproved(
        repo,
        {
          ...request,
          approved: true,
          approvedBy: 'desktop-user',
          decidedAt: new Date().toISOString()
        },
        'git'
      ),
    /changed|invalid/i
  )

  const remoteAfter = execFileSync('git', ['rev-parse', 'refs/heads/main'], { cwd: remote, encoding: 'utf8' }).trim()

  assert.equal(remoteAfter, remoteBefore)
})

for (const remoteSetting of ['url', 'pushurl'] as const) {
  test(`push approval is invalidated when remote ${remoteSetting} changes`, async () => {
    const repo = makeRepo()
    const approvedRemote = attachBareRemote(repo)
    const substitutedRemote = makeBareRemote()

    const approvedRemoteBefore = execFileSync('git', ['rev-parse', 'refs/heads/main'], {
      cwd: approvedRemote,
      encoding: 'utf8'
    }).trim()

    commitChange(repo, `${remoteSetting} attack\n`)
    const request = await reviewCreatePushRequest(repo, 'git')
    execFileSync(
      'git',
      ['remote', 'set-url', ...(remoteSetting === 'pushurl' ? ['--push'] : []), 'origin', substitutedRemote],
      { cwd: repo }
    )

    await assert.rejects(
      () => reviewPushApproved(repo, {
        ...request,
        approved: true,
        approvedBy: 'desktop-user',
        decidedAt: new Date().toISOString()
      }, 'git'),
      /changed|destination|invalid/i
    )

    const approvedRemoteAfter = execFileSync('git', ['rev-parse', 'refs/heads/main'], {
      cwd: approvedRemote,
      encoding: 'utf8'
    }).trim()

    const substitutedRef = spawnSync('git', ['show-ref', '--verify', '--quiet', 'refs/heads/main'], {
      cwd: substitutedRemote
    })

    assert.equal(approvedRemoteAfter, approvedRemoteBefore)
    assert.notEqual(substitutedRef.status, 0)
  })
}

test('legacy commit-and-push is rejected instead of bypassing approval', async () => {
  const repo = makeRepo()
  const remote = attachBareRemote(repo)
  const remoteBefore = execFileSync('git', ['rev-parse', 'refs/heads/main'], { cwd: remote, encoding: 'utf8' }).trim()

  fs.writeFileSync(path.join(repo, 'tracked.txt'), 'must not push\n')
  await assert.rejects(() => reviewCommit(repo, 'unsafe combined action', true, 'git'), /approval/i)

  const remoteAfter = execFileSync('git', ['rev-parse', 'refs/heads/main'], { cwd: remote, encoding: 'utf8' }).trim()

  assert.equal(remoteAfter, remoteBefore)
})
