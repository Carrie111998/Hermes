const { EventEmitter } = require('node:events')

const EVA_APP_UPDATE_FEED =
  'https://github.com/electricsheephq/evaOS-hermes-desktop-app-adapter/releases/latest/download/'
const EVA_APP_UPDATE_CHANNEL = 'latest'
const EVA_APP_UPDATE_BRANCH = 'managed-beta'
const SAFE_CHECK_FAILURE_MESSAGE = 'evaOS Agent could not check for updates. Try again.'
const SAFE_APPLY_FAILURE_MESSAGE = 'evaOS Agent could not install the update. Try again.'
const MANAGED_RELEASE_NOTE_REPLACEMENTS = [
  [/Eva by Electric Sheep/g, 'evaOS Agent'],
  [/Hermes Desktop/g, 'evaOS Agent'],
  [/Hermes Agent/g, 'evaOS Agent'],
  [/Nous Portal/g, 'Electric Sheep account'],
  [/Nous Research/g, 'Electric Sheep'],
  [/\bHermes\b/g, 'evaOS Agent'],
  [/\bEva\b/g, 'evaOS Agent'],
  [/\bNous\b/g, 'Electric Sheep']
]

function normalizeVersion(info) {
  const value = String(info?.version || '').trim()
  return value || null
}

function sanitizeReleaseNote(summary) {
  return MANAGED_RELEASE_NOTE_REPLACEMENTS.reduce(
    (current, [pattern, replacement]) => current.replace(pattern, replacement),
    String(summary || '')
  )
}

function releaseNoteCommits(info, now = Date.now) {
  const raw = Array.isArray(info?.releaseNotes)
    ? info.releaseNotes
        .map(item => item?.note)
        .filter(Boolean)
        .join('\n')
    : String(info?.releaseNotes || '')

  return raw
    .split('\n')
    .map(line => line.trim().replace(/^[-*]\s+/, ''))
    .filter(line => line && !line.startsWith('#'))
    .slice(0, 20)
    .map((summary, index) => ({
      sha: `release-note:${normalizeVersion(info) || 'unknown'}:${index + 1}`,
      summary: sanitizeReleaseNote(summary),
      author: 'Electric Sheep',
      at: now()
    }))
}

function statusFor(app, info, updateAvailable, now = Date.now) {
  const targetVersion = normalizeVersion(info)
  const commits = updateAvailable ? releaseNoteCommits(info, now) : []

  return {
    supported: true,
    updateAvailable,
    branch: EVA_APP_UPDATE_BRANCH,
    currentSha: `release:${app.getVersion()}`,
    targetSha: updateAvailable && targetVersion ? `release:${targetVersion}` : undefined,
    behind: updateAvailable ? Math.max(1, commits.length) : 0,
    commits: commits.length > 0 ? commits : undefined,
    message: updateAvailable && targetVersion ? `evaOS Agent ${targetVersion} is ready to install.` : undefined,
    fetchedAt: now()
  }
}

function unsupportedStatus(message, now = Date.now) {
  return {
    supported: false,
    branch: EVA_APP_UPDATE_BRANCH,
    message,
    fetchedAt: now()
  }
}

function safeCheckFailure(now = Date.now) {
  return {
    supported: true,
    branch: EVA_APP_UPDATE_BRANCH,
    error: 'check-failed',
    message: SAFE_CHECK_FAILURE_MESSAGE,
    fetchedAt: now()
  }
}

function safeApplyFailure() {
  return {
    ok: false,
    error: 'apply-failed',
    message: SAFE_APPLY_FAILURE_MESSAGE
  }
}

function createEvaAppUpdater(options) {
  const {
    app,
    arch = process.arch,
    autoUpdater,
    emitProgress = () => undefined,
    isPackaged = app?.isPackaged,
    now = Date.now,
    onError = () => undefined,
    platform = process.platform,
    prepareInstallHandoff = () => undefined,
    schedule = setTimeout
  } = options || {}

  if (!app || typeof app.getVersion !== 'function') {
    throw new Error('evaOS Agent updater requires an Electron app instance.')
  }

  if (!autoUpdater || !(autoUpdater instanceof EventEmitter) || typeof autoUpdater.setFeedURL !== 'function') {
    throw new Error('evaOS Agent updater requires electron-updater.')
  }

  let lastStatus = null
  let downloadedVersion = null
  let checkPromise = null
  let applyPromise = null
  let applying = false

  function supported() {
    return Boolean(isPackaged) && platform === 'darwin' && arch === 'arm64'
  }

  function unavailableMessage() {
    return Boolean(isPackaged) && platform === 'darwin' && arch !== 'arm64'
      ? 'Signed in-app updates require the Apple Silicon evaOS Agent app.'
      : 'Signed in-app updates are available in the installed macOS app.'
  }

  function configure() {
    autoUpdater.autoDownload = false
    autoUpdater.autoInstallOnAppQuit = false
    autoUpdater.allowPrerelease = true
    // electron-updater's channel setter enables allowDowngrade. Set the
    // channel first, then restore the product's forward-only invariant.
    autoUpdater.channel = EVA_APP_UPDATE_CHANNEL
    autoUpdater.allowDowngrade = false
    autoUpdater.setFeedURL({
      provider: 'generic',
      url: EVA_APP_UPDATE_FEED,
      channel: EVA_APP_UPDATE_CHANNEL
    })
  }

  function reportError(stage, error) {
    try {
      onError(stage, error)
    } catch {
      // Diagnostics must never replace the stable updater result.
    }
  }

  autoUpdater.on('update-available', info => {
    lastStatus = statusFor(app, info, true, now)
  })

  autoUpdater.on('update-not-available', info => {
    lastStatus = statusFor(app, info, false, now)
  })

  autoUpdater.on('download-progress', progress => {
    if (!applying) {
      return
    }

    const percent = Number.isFinite(progress?.percent) ? Math.max(0, Math.min(100, progress.percent)) : null
    emitProgress({
      stage: 'fetch',
      message:
        percent === null ? 'Downloading the signed update…' : `Downloading the signed update… ${Math.round(percent)}%`,
      percent
    })
  })

  autoUpdater.on('update-downloaded', info => {
    downloadedVersion = normalizeVersion(info)
    if (!applying) {
      return
    }

    emitProgress({
      stage: 'restart',
      message: 'Installing the signed update and restarting evaOS Agent…',
      percent: 100
    })
  })

  autoUpdater.on('error', error => {
    if (!applying) {
      return
    }

    reportError('event', error)
    emitProgress({
      stage: 'error',
      message: SAFE_APPLY_FAILURE_MESSAGE,
      error: 'app-update-failed',
      percent: null
    })
  })

  async function check() {
    if (!supported()) {
      return unsupportedStatus(unavailableMessage(), now)
    }

    if (checkPromise) {
      return checkPromise
    }

    checkPromise = (async () => {
      try {
        configure()
        lastStatus = null
        const result = await autoUpdater.checkForUpdates()
        if (!lastStatus) {
          const info = result?.updateInfo
          const version = normalizeVersion(info)
          lastStatus = statusFor(app, info, Boolean(version && version !== app.getVersion()), now)
        }
        return lastStatus
      } catch (error) {
        reportError('check', error)
        return safeCheckFailure(now)
      } finally {
        checkPromise = null
      }
    })()

    return checkPromise
  }

  async function apply() {
    if (!supported()) {
      return {
        ok: false,
        error: 'unavailable',
        message: unavailableMessage()
      }
    }

    if (applyPromise) {
      return applyPromise
    }

    applyPromise = (async () => {
      applying = true
      try {
        configure()
        const status = lastStatus?.updateAvailable ? lastStatus : await check()
        if (!status?.updateAvailable) {
          return {
            ok: false,
            error: status?.error || 'no-update',
            message: status?.message || 'evaOS Agent is already up to date.'
          }
        }

        downloadedVersion = null
        emitProgress({ stage: 'fetch', message: 'Downloading the signed update…', percent: 0 })
        await autoUpdater.downloadUpdate()

        if (!downloadedVersion) {
          throw new Error('The update downloaded without a verified release identity.')
        }

        schedule(() => {
          let rollbackHandoff
          try {
            rollbackHandoff = prepareInstallHandoff()
            autoUpdater.quitAndInstall(false, true)
          } catch (error) {
            if (typeof rollbackHandoff === 'function') {
              rollbackHandoff()
            }
            reportError('install-handoff', error)
            emitProgress({
              stage: 'error',
              message: SAFE_APPLY_FAILURE_MESSAGE,
              error: 'app-update-failed',
              percent: null
            })
          }
        }, 500)
        return { ok: true, handedOff: true, message: `Installing evaOS Agent ${downloadedVersion}.` }
      } catch (error) {
        reportError('apply', error)
        emitProgress({
          stage: 'error',
          message: SAFE_APPLY_FAILURE_MESSAGE,
          error: 'app-update-failed',
          percent: null
        })
        return safeApplyFailure()
      } finally {
        applying = false
        applyPromise = null
      }
    })()

    return applyPromise
  }

  return Object.freeze({
    apply,
    check,
    feedUrl: EVA_APP_UPDATE_FEED
  })
}

module.exports = {
  EVA_APP_UPDATE_BRANCH,
  EVA_APP_UPDATE_CHANNEL,
  EVA_APP_UPDATE_FEED,
  createEvaAppUpdater,
  releaseNoteCommits,
  safeApplyFailure,
  safeCheckFailure,
  sanitizeReleaseNote,
  statusFor,
  unsupportedStatus
}
