import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import { createDesktopUpdateChecks } from './desktop-update-checks'

const fixtureRoots = new Set<string>()

afterEach(() => {
  for (const root of fixtureRoots) {
    fs.rmSync(root, { force: true, recursive: true })
  }

  fixtureRoots.clear()
})

function fixture(overrides: Record<string, any> = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-update-checks-'))
  fixtureRoots.add(root)
  const configPath = path.join(root, 'desktop-update.json')
  const logs: string[] = []
  const sent: Array<{ channel: string; payload: any }> = []

  const windows = [
    {
      webContents: {
        send: (channel: string, payload: any) => sent.push({ channel, payload })
      }
    }
  ]

  const deps = {
    ACTIVE_HERMES_ROOT: path.join(root, 'active'),
    BrowserWindow: { getAllWindows: () => windows },
    DEFAULT_UPDATE_BRANCH: 'main',
    DESKTOP_UPDATE_CONFIG_PATH: configPath,
    HERMES_HOME: root,
    IS_PACKAGED: true,
    IS_WINDOWS: false,
    OFFICIAL_REPO_HTTPS_URL: 'https://github.com/NousResearch/hermes-agent.git',
    SOURCE_REPO_ROOT: path.join(root, 'source'),
    clearStaleGitLocks: async () => undefined,
    compareApiUrl: () => null,
    directoryExists: () => false,
    fs,
    https: {},
    isHermesSourceRoot: () => false,
    isOfficialSshRemote: () => false,
    path,
    parseCompareBehindCount: () => null,
    rememberLog: (message: string) => logs.push(message),
    resolveBehindCount: () => null,
    resolveCommitLogSelection: () => ({ limit: 20, revision: 'HEAD' }),
    resolveGitBinary: () => 'git',
    shouldCountCommits: () => false,
    spawn: () => {
      throw new Error('spawn should not be reached by this fixture')
    },
    hiddenWindowsChildOptions: (options: any) => options,
    writeFileAtomic: (targetPath: string, data: string, encoding?: BufferEncoding) =>
      fs.writeFileSync(targetPath, data, encoding),
    ...overrides
  }

  return { root, logs, sent, shard: createDesktopUpdateChecks(deps) }
}

test('legacy update names resolve to the extracted function objects', () => {
  const { shard } = fixture()

  const legacy = {
    checkUpdates: shard.checkUpdates,
    emitUpdateProgress: shard.emitUpdateProgress,
    fetchCompareBehindCount: shard.fetchCompareBehindCount,
    firstLine: shard.firstLine,
    getOriginUrl: shard.getOriginUrl,
    readCommitLog: shard.readCommitLog,
    readDesktopUpdateConfig: shard.readDesktopUpdateConfig,
    resolveHealedBranch: shard.resolveHealedBranch,
    resolveUpdateRoot: shard.resolveUpdateRoot,
    runGit: shard.runGit,
    writeDesktopUpdateConfig: shard.writeDesktopUpdateConfig
  }

  for (const name of Object.keys(legacy) as Array<keyof typeof legacy>) {
    assert.strictEqual(legacy[name], shard[name])
    assert.equal(typeof legacy[name], 'function')
  }
})

test('update branch configuration preserves the main-process read/write contract', () => {
  const { shard } = fixture()

  assert.deepEqual(shard.readDesktopUpdateConfig(), { branch: 'main' })
  shard.writeDesktopUpdateConfig({ branch: 'release-candidate' })
  assert.deepEqual(shard.readDesktopUpdateConfig(), { branch: 'release-candidate' })
})

test('update-root resolution prefers an explicit checkout override', () => {
  const root = path.join(os.tmpdir(), 'hermes-update-root-override')
  const previous = process.env.HERMES_DESKTOP_HERMES_ROOT
  process.env.HERMES_DESKTOP_HERMES_ROOT = root

  try {
    const { shard } = fixture({
      directoryExists: (candidate: string) => candidate === path.join(root, '.git')
    })

    assert.equal(shard.resolveUpdateRoot(), path.resolve(root))
  } finally {
    if (previous === undefined) {
      delete process.env.HERMES_DESKTOP_HERMES_ROOT
    } else {
      process.env.HERMES_DESKTOP_HERMES_ROOT = previous
    }
  }
})

test('not-a-git update roots retain the exact unsupported result shape', async () => {
  const { shard } = fixture()
  const result = await shard.checkUpdates()

  assert.deepEqual(result, {
    supported: false,
    reason: 'not-a-git-checkout',
    message: `${result.hermesRoot} isn't a git checkout — desktop self-update only runs against a source install.`,
    hermesRoot: result.hermesRoot,
    branch: 'main'
  })
})

test('progress emits the legacy log and renderer notification', () => {
  const { shard, logs, sent } = fixture()

  shard.emitUpdateProgress({ stage: 'manual', message: 'hermes update', percent: null })

  assert.deepEqual(logs, ['[updates] manual: hermes update'])
  assert.equal(sent.length, 1)
  assert.equal(sent[0].channel, 'hermes:updates:progress')
  assert.deepEqual({ ...sent[0].payload, at: 0 }, {
    stage: 'manual',
    message: 'hermes update',
    percent: null,
    error: null,
    at: 0
  })
})

test('firstLine keeps the first non-empty line for update diagnostics', () => {
  const { shard } = fixture()

  assert.equal(shard.firstLine('\n\nfirst\nsecond'), 'first')
  assert.equal(shard.firstLine(''), '')
})
