const assert = require('node:assert/strict')
const test = require('node:test')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const {
  runBootstrap,
  resolveInstallScript,
  installedAgentInstallScript,
  cachedScriptPath
} = require('./bootstrap-runner.cjs')

const SCRIPT_NAME = process.platform === 'win32' ? 'install.ps1' : 'install.sh'

function mkTmpHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-bootstrap-test-'))
}

test('runBootstrap bails immediately when the signal is already aborted', async () => {
  const controller = new AbortController()
  controller.abort()

  const events = []
  const result = await runBootstrap({
    installStamp: null,
    activeRoot: '/tmp/hermes-runner-test',
    sourceRepoRoot: null,
    hermesHome: '/tmp/hermes-runner-test',
    logRoot: '/tmp/hermes-runner-test',
    onEvent: ev => events.push(ev),
    abortSignal: controller.signal
  })

  // Cancelled before any install script is spawned.
  assert.deepEqual(result, { ok: false, cancelled: true })
  assert.ok(
    events.some(ev => ev.type === 'failed' && /cancelled/i.test(ev.error)),
    'should emit a cancelled failure event'
  )
})

test('installedAgentInstallScript resolves the installer in the agent checkout', () => {
  const home = mkTmpHome()
  try {
    assert.equal(installedAgentInstallScript(home), null, 'absent before the checkout exists')

    const scriptsDir = path.join(home, 'hermes-agent', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    const scriptPath = path.join(scriptsDir, SCRIPT_NAME)
    fs.writeFileSync(scriptPath, '#!/bin/sh\necho hi\n')

    assert.equal(installedAgentInstallScript(home), scriptPath)
    assert.equal(installedAgentInstallScript(null), null, 'null home -> null')
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript prefers a cached script without touching the network', async () => {
  const home = mkTmpHome()
  try {
    const commit = 'a'.repeat(40)
    const cached = cachedScriptPath(home, commit)
    fs.mkdirSync(path.dirname(cached), { recursive: true })
    fs.writeFileSync(cached, '#!/bin/sh\necho cached\n')

    const logs = []
    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: ev => logs.push(ev)
    })

    assert.equal(result.source, 'cache')
    assert.equal(result.path, cached)
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript falls back to the installed agent checkout on a 404', async () => {
  const home = mkTmpHome()
  try {
    const commit = 'a'.repeat(40)
    // Seed the installed agent checkout so the fallback has something to resolve.
    const scriptsDir = path.join(home, 'hermes-agent', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    const installed = path.join(scriptsDir, SCRIPT_NAME)
    fs.writeFileSync(installed, '#!/bin/sh\necho fallback\n')

    const logs = []
    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: ev => logs.push(ev),
      // Simulate GitHub returning a 404 for the pinned commit.
      _download: async () => {
        throw new Error('Failed to download install.sh: HTTP 404')
      }
    })

    assert.equal(result.source, 'installed-agent')
    // It should have copied the installer into the bootstrap cache.
    assert.equal(result.path, cachedScriptPath(home, commit))
    assert.ok(fs.existsSync(result.path), 'fallback script copied into cache')
    assert.ok(
      logs.some(ev => /falling back to installed agent/.test(ev.line || '')),
      'emits a fallback log line'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript surfaces an actionable error when the 404 fallback is unavailable', async () => {
  const home = mkTmpHome()
  try {
    const commit = 'b'.repeat(40)
    // No installed agent checkout seeded -> nothing to fall back to. A 404 on
    // a pinned SHA is permanent (raw URLs are immutable), so the error must
    // tell the user what to actually do, not just report the HTTP status.
    await assert.rejects(
      resolveInstallScript({
        installStamp: { commit },
        sourceRepoRoot: null,
        hermesHome: home,
        emit: () => {},
        _download: async () => {
          const err = new Error('Failed to download install.sh: HTTP 404')
          err.statusCode = 404
          throw err
        }
      }),
      err => {
        assert.equal(err.installScriptUnavailable, true, 'must be tagged so main.cjs can latch it as permanent')
        assert.match(err.message, /not available upstream/i)
        assert.match(err.message, /HERMES_DESKTOP_HERMES_ROOT/)
        assert.match(err.message, /reinstall/i)
        return true
      }
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript rethrows non-404 download failures untouched', async () => {
  const home = mkTmpHome()
  try {
    const commit = 'c'.repeat(40)
    // Transient network failures (offline, DNS, 5xx) stay retryable -- they
    // must NOT be dressed up as the permanent local-only-commit error.
    await assert.rejects(
      resolveInstallScript({
        installStamp: { commit },
        sourceRepoRoot: null,
        hermesHome: home,
        emit: () => {},
        _download: async () => {
          const err = new Error('getaddrinfo ENOTFOUND raw.githubusercontent.com')
          err.code = 'ENOTFOUND'
          throw err
        }
      }),
      err => {
        assert.notEqual(err.installScriptUnavailable, true)
        assert.match(err.message, /ENOTFOUND/)
        return true
      }
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('resolveInstallScript does not re-fetch a commit that already 404ed this session', async () => {
  const home = mkTmpHome()
  try {
    const commit = 'd'.repeat(40)
    let downloads = 0
    const download404 = async () => {
      downloads++
      const err = new Error('Failed to download install.sh: HTTP 404')
      err.statusCode = 404
      throw err
    }
    const attempt = () =>
      resolveInstallScript({
        installStamp: { commit },
        sourceRepoRoot: null,
        hermesHome: home,
        emit: () => {},
        _download: download404
      })

    await assert.rejects(attempt(), err => err.installScriptUnavailable === true)
    // Renderer "Reload and retry" re-runs the resolver; the identical raw
    // GitHub URL is immutable, so hitting it again can only 404 again.
    await assert.rejects(attempt(), err => err.installScriptUnavailable === true)
    assert.equal(downloads, 1, 'the identical fetch must not be re-attempted')
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('a 404-memoized commit still picks up the installed-agent fallback on retry', async () => {
  const home = mkTmpHome()
  try {
    const commit = 'e'.repeat(40)
    await assert.rejects(
      resolveInstallScript({
        installStamp: { commit },
        sourceRepoRoot: null,
        hermesHome: home,
        emit: () => {},
        _download: async () => {
          const err = new Error('Failed to download install.sh: HTTP 404')
          err.statusCode = 404
          throw err
        }
      }),
      err => err.installScriptUnavailable === true
    )

    // User installs the agent checkout between attempts; retry must succeed
    // via the fallback even though the fetch itself is skipped.
    const scriptsDir = path.join(home, 'hermes-agent', 'scripts')
    fs.mkdirSync(scriptsDir, { recursive: true })
    fs.writeFileSync(path.join(scriptsDir, SCRIPT_NAME), '#!/bin/sh\necho fallback\n')

    const result = await resolveInstallScript({
      installStamp: { commit },
      sourceRepoRoot: null,
      hermesHome: home,
      emit: () => {},
      _download: async () => {
        throw new Error('must not be called for a memoized 404')
      }
    })
    assert.equal(result.source, 'installed-agent')
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})

test('runBootstrap propagates installScriptUnavailable in its failure result', async () => {
  const home = mkTmpHome()
  try {
    const commit = 'f'.repeat(40)
    // Seed the 404 memo through the injectable path, then drive the real
    // runBootstrap entrypoint: it fails fast on the memoized 404 without
    // touching the network, and the flag must survive into the result so
    // main.cjs can surface the actionable message instead of the generic
    // "check the log and retry" wrapper.
    await assert.rejects(
      resolveInstallScript({
        installStamp: { commit },
        sourceRepoRoot: null,
        hermesHome: home,
        emit: () => {},
        _download: async () => {
          const err = new Error('Failed to download install.sh: HTTP 404')
          err.statusCode = 404
          throw err
        }
      }),
      err => err.installScriptUnavailable === true
    )

    const events = []
    const result = await runBootstrap({
      installStamp: { commit },
      activeRoot: path.join(home, 'hermes-agent'),
      sourceRepoRoot: null,
      hermesHome: home,
      logRoot: path.join(home, 'logs'),
      onEvent: ev => events.push(ev)
    })

    assert.equal(result.ok, false)
    assert.equal(result.installScriptUnavailable, true)
    assert.match(result.error, /HERMES_DESKTOP_HERMES_ROOT/)
    assert.ok(
      events.some(ev => ev.type === 'failed' && /HERMES_DESKTOP_HERMES_ROOT/.test(ev.error || '')),
      'failed event carries the actionable message to the renderer overlay'
    )
  } finally {
    fs.rmSync(home, { recursive: true, force: true })
  }
})
