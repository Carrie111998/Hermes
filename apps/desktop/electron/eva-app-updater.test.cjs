const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const test = require('node:test')

const {
  EVA_APP_UPDATE_FEED,
  createEvaAppUpdater,
  releaseNoteCommits,
  safeApplyFailure,
  safeCheckFailure,
  sanitizeReleaseNote,
  statusFor,
  unsupportedStatus
} = require('./eva-app-updater.cjs')

class FakeUpdater extends EventEmitter {
  constructor() {
    super()
    this.feedCalls = []
    this.checkCalls = 0
    this.downloadCalls = 0
    this.installCalls = []
    this._channel = null
  }

  get channel() {
    return this._channel
  }

  set channel(value) {
    this._channel = value
    // Match electron-updater AppUpdater: selecting a channel implicitly opts
    // into downgrade unless the caller restores allowDowngrade afterwards.
    this.allowDowngrade = true
  }

  setFeedURL(value) {
    this.feedCalls.push(value)
  }

  async checkForUpdates() {
    this.checkCalls += 1
    this.emit('checking-for-update')
    this.emit('update-available', { version: '2026.7.20-es.9' })
    return { updateInfo: { version: '2026.7.20-es.9' } }
  }

  async downloadUpdate() {
    this.downloadCalls += 1
    this.emit('download-progress', { percent: 42.4 })
    this.emit('update-downloaded', { version: '2026.7.20-es.9' })
    return ['/tmp/evaos-agent.zip']
  }

  quitAndInstall(...args) {
    this.installCalls.push(args)
  }
}

function fixture(overrides = {}) {
  const updater = new FakeUpdater()
  const progress = []
  const scheduled = []
  const handoffCalls = []
  const errors = []
  const service = createEvaAppUpdater({
    app: { getVersion: () => '2026.7.20-es.8', isPackaged: true },
    arch: 'arm64',
    autoUpdater: updater,
    emitProgress: value => progress.push(value),
    isPackaged: true,
    now: () => 1234,
    onError: (stage, error) => errors.push({ error, stage }),
    platform: 'darwin',
    prepareInstallHandoff: () => {
      handoffCalls.push('prepare')

      return () => handoffCalls.push('rollback')
    },
    schedule: callback => {
      scheduled.push(callback)
      return scheduled.length
    },
    ...overrides
  })
  return { errors, handoffCalls, progress, scheduled, service, updater }
}

async function waitForScheduled(scheduled, count) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (scheduled.length >= count) {
      return
    }
    await new Promise(resolve => setImmediate(resolve))
  }
  throw new Error(`Expected ${count} scheduled updater handoff(s); received ${scheduled.length}.`)
}

test('managed app updater always restores the fixed Electric Sheep feed and forward-only policy', async () => {
  const { progress, service, updater } = fixture()

  const status = await service.check()
  assert.equal(status.supported, true)
  assert.equal(status.updateAvailable, true)
  assert.equal(status.currentSha, 'release:2026.7.20-es.8')
  assert.equal(status.targetSha, 'release:2026.7.20-es.9')
  assert.deepEqual(updater.feedCalls.at(-1), {
    provider: 'generic',
    url: EVA_APP_UPDATE_FEED,
    channel: 'latest'
  })
  assert.equal(updater.autoDownload, false)
  assert.equal(updater.autoInstallOnAppQuit, false)
  assert.equal(updater.allowDowngrade, false)
  assert.equal(updater.allowPrerelease, true)
  assert.equal(updater.channel, 'latest')
  assert.deepEqual(progress, [])
})

test('release notes become normal update-overlay entries without executable content', () => {
  const commits = releaseNoteCommits(
    {
      version: '2026.7.20-es.9',
      releaseNotes: '# evaOS Agent\n\n- Fixes remote attachments.\n- Adds signed updates.'
    },
    () => 1234
  )
  assert.deepEqual(commits, [
    {
      sha: 'release-note:2026.7.20-es.9:1',
      summary: 'Fixes remote attachments.',
      author: 'Electric Sheep',
      at: 1234
    },
    {
      sha: 'release-note:2026.7.20-es.9:2',
      summary: 'Adds signed updates.',
      author: 'Electric Sheep',
      at: 1234
    }
  ])
})

test('exported updater helpers use a live clock by default', () => {
  const commits = releaseNoteCommits({ version: '2026.7.20-es.9', releaseNotes: '- Fixed.' })
  const status = statusFor({ getVersion: () => '2026.7.20-es.8' }, { version: '2026.7.20-es.9' }, true)
  const unsupported = unsupportedStatus('Unavailable.')

  assert.equal(Number.isFinite(commits[0].at), true)
  assert.equal(Number.isFinite(status.fetchedAt), true)
  assert.equal(Number.isFinite(unsupported.fetchedAt), true)
  assert.equal(safeCheckFailure().message, 'evaOS Agent could not check for updates. Try again.')
  assert.equal(safeApplyFailure().message, 'evaOS Agent could not install the update. Try again.')
})

test('release notes cannot restore upstream product branding', () => {
  assert.equal(
    sanitizeReleaseNote('Hermes Desktop by Nous Research connects through Nous Portal for Eva'),
    'evaOS Agent by Electric Sheep connects through Electric Sheep account for evaOS Agent'
  )

  const commits = releaseNoteCommits(
    {
      version: '2026.7.20-es.9',
      releaseNotes: '- Update Hermes Agent.\n- Fix Nous Portal sign-in.'
    },
    () => 1234
  )

  assert.deepEqual(
    commits.map(commit => commit.summary),
    ['Update evaOS Agent.', 'Fix Electric Sheep account sign-in.']
  )
})

test('apply downloads, reports progress, and prepares handoff before the signed restart', async () => {
  const { handoffCalls, progress, scheduled, service, updater } = fixture()

  const resultPromise = service.apply()
  await waitForScheduled(scheduled, 1)
  scheduled[0]()
  const result = await resultPromise
  assert.deepEqual(result, {
    ok: true,
    handedOff: true,
    message: 'Installing evaOS Agent 2026.7.20-es.9.'
  })
  assert.equal(updater.checkCalls, 1)
  assert.equal(updater.downloadCalls, 1)
  assert.equal(scheduled.length, 1)
  assert.deepEqual(updater.installCalls, [[false, true]])
  assert.equal(
    progress.some(item => item.stage === 'fetch' && item.percent === 42.4),
    true
  )
  assert.equal(
    progress.some(item => item.stage === 'restart' && item.percent === 100),
    true
  )
  assert.deepEqual(handoffCalls, ['prepare'])
})

test('apply remains coalesced until the scheduled install handoff completes', async () => {
  const { scheduled, service, updater } = fixture()

  const first = service.apply()
  await waitForScheduled(scheduled, 1)
  const second = service.apply()
  await new Promise(resolve => setImmediate(resolve))

  assert.equal(updater.downloadCalls, 1)
  assert.equal(scheduled.length, 1)

  scheduled[0]()
  const [firstResult, secondResult] = await Promise.all([first, second])
  assert.deepEqual(secondResult, firstResult)
})

test('a failed install handoff restores the active-work quit guard and reports a safe error', async () => {
  const { errors, handoffCalls, progress, scheduled, service, updater } = fixture()
  updater.quitAndInstall = () => {
    throw new Error('helper failed at /private/tmp/secret')
  }

  const resultPromise = service.apply()
  await waitForScheduled(scheduled, 1)
  scheduled[0]()
  const result = await resultPromise

  assert.deepEqual(result, safeApplyFailure())
  assert.deepEqual(handoffCalls, ['prepare', 'rollback'])
  assert.equal(errors.at(-1).stage, 'apply')
  assert.equal(errors.at(-1).error.message, 'helper failed at /private/tmp/secret')
  assert.equal(progress.at(-1).message, 'evaOS Agent could not install the update. Try again.')
  assert.equal(progress.at(-1).message.includes('/private/tmp'), false)
})

test('unpacked or non-mac builds never check, download, or install', async () => {
  const { service, updater } = fixture({ isPackaged: false })

  const check = await service.check()
  const apply = await service.apply()
  assert.equal(check.supported, false)
  assert.equal(apply.ok, false)
  assert.equal(apply.error, 'unavailable')
  assert.equal(updater.checkCalls, 0)
  assert.equal(updater.downloadCalls, 0)
  assert.deepEqual(updater.installCalls, [])
})

test('packaged Intel macOS builds cannot consume the arm64-only managed appcast', async () => {
  const { service, updater } = fixture({ arch: 'x64' })

  const check = await service.check()
  const apply = await service.apply()

  assert.equal(check.supported, false)
  assert.equal(check.message, 'Signed in-app updates require the Apple Silicon evaOS Agent app.')
  assert.equal(apply.ok, false)
  assert.equal(apply.error, 'unavailable')
  assert.equal(apply.message, check.message)
  assert.equal(updater.checkCalls, 0)
  assert.equal(updater.downloadCalls, 0)
  assert.deepEqual(updater.installCalls, [])
})

test('a no-update apply clears state so a later update installs in the same process', async () => {
  const updater = new FakeUpdater()
  updater.checkForUpdates = async function () {
    this.checkCalls += 1
    const info = this.checkCalls === 1 ? { version: '2026.7.20-es.8' } : { version: '2026.7.20-es.9' }
    this.emit(this.checkCalls === 1 ? 'update-not-available' : 'update-available', info)

    return { updateInfo: info }
  }
  const { scheduled, service } = fixture({ autoUpdater: updater })

  const first = await service.apply()
  const secondPromise = service.apply()
  await waitForScheduled(scheduled, 1)
  scheduled[0]()
  const second = await secondPromise

  assert.deepEqual(first, {
    ok: false,
    error: 'no-update',
    message: 'evaOS Agent is already up to date.'
  })
  assert.equal(second.ok, true)
  assert.equal(updater.checkCalls, 2)
  assert.equal(updater.downloadCalls, 1)
  assert.equal(scheduled.length, 1)
})

test('check failures remain renderer-safe and a later apply retries in the same process', async () => {
  const updater = new FakeUpdater()
  updater.checkForUpdates = async function () {
    this.checkCalls += 1
    if (this.checkCalls === 1) {
      throw new Error('feed unavailable at https://private.example.invalid/latest-mac.yml')
    }
    const info = { version: '2026.7.20-es.9' }
    this.emit('update-available', info)

    return { updateInfo: info }
  }
  const { errors, scheduled, service } = fixture({ autoUpdater: updater })

  const first = await service.apply()
  const secondPromise = service.apply()
  await waitForScheduled(scheduled, 1)
  scheduled[0]()
  const second = await secondPromise

  assert.deepEqual(first, {
    ok: false,
    error: 'check-failed',
    message: 'evaOS Agent could not check for updates. Try again.'
  })
  assert.equal(first.message.includes('private.example.invalid'), false)
  assert.equal(errors[0].error.message.includes('private.example.invalid'), true)
  assert.equal(second.ok, true)
  assert.equal(updater.downloadCalls, 1)
  assert.equal(scheduled.length, 1)
})

test('a check without update info does not invent an available target', async () => {
  const updater = new FakeUpdater()
  updater.checkForUpdates = async function () {
    this.checkCalls += 1

    return {}
  }
  const { service } = fixture({ autoUpdater: updater })

  const status = await service.check()
  assert.equal(status.updateAvailable, false)
  assert.equal(status.targetSha, undefined)
  assert.equal(status.behind, 0)
})

test('download failures keep infrastructure details out of renderer results and progress', async () => {
  const updater = new FakeUpdater()
  updater.downloadUpdate = async function () {
    this.downloadCalls += 1
    throw new Error('signature failed for /private/tmp/evaOS-Agent.zip')
  }
  const { errors, progress, service } = fixture({ autoUpdater: updater })

  const result = await service.apply()

  assert.deepEqual(result, safeApplyFailure())
  assert.equal(result.message.includes('/private/tmp'), false)
  assert.equal(progress.at(-1).message, result.message)
  assert.equal(errors.at(-1).error.message.includes('/private/tmp'), true)
})
