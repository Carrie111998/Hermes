/**
 * electron-updater controller — the self-update path for packaged remote
 * clients (always-on backend host + a client with no repo/CLI/build tools).
 *
 * Kept OUT of main.ts so the god-file stays thin and the policy is readable
 * in one place (the same extraction the update-strategy ladder and the
 * pool-reaper module follow). main.ts decides WHETHER this rung applies via
 * resolveUpdateStrategy(); this module only drives the mechanism when it
 * does.
 *
 * Lifecycle
 * ---------
 *   configureElectronUpdater({ feedUrl, log, onProgress })
 *     → wires the feed + event handlers once at startup.
 *   checkForUpdates()
 *     → electron-updater queries the feed; on a newer version it downloads
 *       the signed artifact in the background and emits onProgress stages.
 *   quitAndInstall()
 *     → restarts into the downloaded version (user-confirmed from the UI).
 *
 * electron-updater keeps the previously-installed version on disk, so a bad
 * update is recoverable by relaunching the old bundle — that is the rollback
 * affordance.
 *
 * Safety
 * ------
 * This module is ONLY reachable for a packaged app with a configured feed
 * (the update-strategy ladder's top rung). It never touches the source /
 * hermes-CLI paths those installs use. autoDownload is left enabled for the
 * frictionless remote-client case; the UI still gates the actual install
 * behind an explicit user action via quitAndInstall().
 */

import { autoUpdater, type UpdateInfo, type ProgressInfo } from 'electron-updater'

import { feedConfiguration } from './update-strategy'

export interface ElectronUpdaterHandlers {
  /** Progress/milestone reporter — forwarded to main.ts's emitUpdateProgress. */
  onProgress: (payload: { stage: string; message: string; percent: number | null; error?: string | null }) => void
  /** Free-form log line — forwarded to main.ts's rememberLog. */
  log: (line: string) => void
}

export interface ElectronUpdaterConfig extends ElectronUpdaterHandlers {
  /** Resolved feed URL (updates.json `feed_url`). Required — the caller gates on it. */
  feedUrl: string
}

let configured = false
let downloadedVersion: string | null = null

/**
 * Wire the feed + event handlers. Idempotent: calling twice only re-applies
 * the feed URL, it does not register duplicate listeners.
 */
export function configureElectronUpdater(config: ElectronUpdaterConfig): void {
  const { feedUrl, onProgress, log } = config

  autoUpdater.setFeedURL(feedConfiguration(feedUrl))
  autoUpdater.autoDownload = true
  autoUpdater.autoInstallOnAppQuit = false // install only on explicit user action

  if (configured) {
    log(`[updates] electron-updater re-configured (feed=${feedUrl})`)
    return
  }
  configured = true

  autoUpdater.on('checking-for-update', () => {
    log('[updates] electron-updater: checking for update')
    onProgress({ stage: 'check', message: 'Checking for updates…', percent: null })
  })

  autoUpdater.on('update-available', (info: UpdateInfo) => {
    log(`[updates] electron-updater: update available ${info.version}`)
    onProgress({ stage: 'download', message: `Downloading Hermes ${info.version}…`, percent: 0 })
  })

  autoUpdater.on('update-not-available', () => {
    log('[updates] electron-updater: already up to date')
    onProgress({ stage: 'idle', message: 'Hermes is up to date.', percent: null })
  })

  autoUpdater.on('download-progress', (p: ProgressInfo) => {
    onProgress({
      stage: 'download',
      message: `Downloading update… ${Math.round(p.percent)}%`,
      percent: Math.round(p.percent)
    })
  })

  autoUpdater.on('update-downloaded', (info: UpdateInfo) => {
    downloadedVersion = info.version
    log(`[updates] electron-updater: downloaded ${info.version}; awaiting install`)
    onProgress({
      stage: 'ready',
      message: `Hermes ${info.version} is ready — restart to update.`,
      percent: 100
    })
  })

  autoUpdater.on('error', (err: Error) => {
    log(`[updates] electron-updater error: ${err?.message || String(err)}`)
    onProgress({
      stage: 'error',
      message: 'Update failed — the current version keeps working.',
      percent: null,
      error: err?.message || String(err)
    })
  })
}

/** Query the feed for a newer version (and download it in the background).
 *  Returns electron-updater's check result so the detection path
 *  (`checkUpdates`) can compare feed vs running version synchronously; the
 *  event handlers keep driving the progress stream either way. */
export async function checkForUpdates(): Promise<{ updateInfo?: { version?: string } } | null> {
  return await autoUpdater.checkForUpdates()
}

/**
 * Restart into the downloaded update. Resolves false when no update has been
 * downloaded yet (the UI should not offer "restart to update" in that state).
 */
export function quitAndInstall(): boolean {
  if (!downloadedVersion) {
    return false
  }
  // isSilent=false keeps the OS installer UX; isForceRunAfter relaunches.
  autoUpdater.quitAndInstall(false, true)
  return true
}

/** Version downloaded and awaiting install (null when none). Read-only for the UI. */
export function pendingUpdateVersion(): string | null {
  return downloadedVersion
}
