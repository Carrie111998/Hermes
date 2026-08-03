const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const test = require('node:test')

const {
  EVA_APP_UPDATE_FEED,
  createEvaAppUpdater,
  releaseNoteCommits,
  sanitizeReleaseNote
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
  const service = createEvaAppUpdater({
    app: { getVersion: () => '2026.7.20-es.8', isPackaged: true },
    autoUpdater: updater,
    emitProgress: value => progress.push(value),
    isPackaged: true,
    now: () => 1234,
    platform: 'darwin',
    schedule: callback => {
      scheduled.push(callback)
      return scheduled.length
    },
    ...overrides
  })
  return { progress, scheduled, service, updater }
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

test('apply downloads, reports progress, and schedules a signed restart handoff', async () => {
  const { progress, scheduled, service, updater } = fixture()

  const result = await service.apply()
  assert.deepEqual(result, {
    ok: true,
    handedOff: true,
    message: 'Installing evaOS Agent 2026.7.20-es.9.'
  })
  assert.equal(updater.checkCalls, 1)
  assert.equal(updater.downloadCalls, 1)
  assert.equal(scheduled.length, 1)
  assert.deepEqual(updater.installCalls, [])
  assert.equal(
    progress.some(item => item.stage === 'fetch' && item.percent === 42.4),
    true
  )
  assert.equal(
    progress.some(item => item.stage === 'restart' && item.percent === 100),
    true
  )

  scheduled[0]()
  assert.deepEqual(updater.installCalls, [[false, true]])
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

test('check failures remain recoverable and do not attempt installation', async () => {
  const updater = new FakeUpdater()
  updater.checkForUpdates = async () => {
    throw new Error('feed unavailable')
  }
  const { service } = fixture({ autoUpdater: updater })

  const status = await service.check()
  const result = await service.apply()
  assert.equal(status.error, 'check-failed')
  assert.equal(result.ok, false)
  assert.equal(result.error, 'check-failed')
  assert.equal(updater.downloadCalls, 0)
  assert.deepEqual(updater.installCalls, [])
})
