import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { zipSync } from 'fflate'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  applyCompatibilityPatches,
  canonicalArchiveUrl,
  createPreviewUblockInstaller,
  extractArchive,
  PREVIEW_UBLOCK_RELEASE_API,
  validateExtensionDirectory
} from './preview-ublock-installer'

const TAG = '2026.825.1619'
const temporaryDirectories: string[] = []

function temporaryDirectory(): string {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-preview-ublock-'))
  temporaryDirectories.push(directory)

  return directory
}

function archiveFiles(version = TAG): Uint8Array {
  const background = `browser.permissions.onRemoved.addListener((...args) => {
        isFullyInitialized.then(( ) => {
            onPermissionsChanged('removed', ...args);
        });
    });
browser.permissions.onAdded.addListener((...args) => {
        isFullyInitialized.then(( ) => {
            onPermissionsChanged('added', ...args);
        });
    });
browser.commands.onCommand.addListener((...args) => {
        isFullyInitialized.then(( ) => {
            onCommand(...args);
        });
    });`

  const extUtils = `export async function hasBroadHostPermissions() {
    return browser.permissions.getAll().then(permissions =>
        permissions.origins.includes('<all_urls>')
    );
}`

  const modeManager = `async function getBrowserPermissions() {
    return browser.permissions.getAll();
}

export async function persistHostPermissions(iter) {
    if ( iter === undefined ) {
        const permissions = await browser.permissions.getAll();
            iter = hostnamesFromMatches(permissions.origins) || [];
    }
}`

  const manifest = JSON.stringify({
    background: { service_worker: '/js/background.js' },
    manifest_version: 3,
    name: 'uBlock Origin Lite',
    version
  })

  return zipSync({
    'LICENSE.txt': new TextEncoder().encode('MPL-2.0'),
    'dashboard.html': new TextEncoder().encode('<!doctype html>'),
    'js/background.js': new TextEncoder().encode(background),
    'js/ext-utils.js': new TextEncoder().encode(extUtils),
    'js/mode-manager.js': new TextEncoder().encode(modeManager),
    'manifest.json': new TextEncoder().encode(manifest),
    'rulesets/main/easylist.json': new TextEncoder().encode('[]'),
    'rulesets/main/easyprivacy.json': new TextEncoder().encode('[]'),
    'rulesets/main/ublock-filters.json': new TextEncoder().encode('[]')
  })
}

function releaseFor(archive: Uint8Array, tag = TAG) {
  const digest = Buffer.from(awaitableDigest(archive)).toString('hex')

  return {
    assets: [
      {
        browser_download_url: canonicalArchiveUrl(tag),
        content_type: 'application/zip',
        digest: `sha256:${digest}`,
        name: `uBOLite_${tag}.chromium.zip`,
        size: archive.length,
        state: 'uploaded'
      }
    ],
    draft: false,
    prerelease: false,
    tag_name: tag
  }
}

function awaitableDigest(value: Uint8Array): Uint8Array {
  const hash = new Uint8Array(32)
  Buffer.from(crypto.createHash('sha256').update(value).digest()).copy(hash)

  return hash
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    fs.rmSync(directory, { force: true, recursive: true })
  }
})

describe('preview uBlock installer', () => {
  it('validates a release, applies Electron patches, and reuses a valid cache', async () => {
    const archive = archiveFiles()
    const release = releaseFor(archive)

    const request = vi
      .fn()
      .mockResolvedValueOnce(new TextEncoder().encode(JSON.stringify(release)))
      .mockResolvedValueOnce(archive)

    const installer = createPreviewUblockInstaller({ request, userDataPath: temporaryDirectory() })

    const installed = await installer.resolve('latest')
    expect(installed?.version).toBe(TAG)
    expect(fs.existsSync(path.join(installed!.path, 'manifest.json'))).toBe(true)
    expect(await installer.resolve('cached')).toEqual(installed)
    expect(request).toHaveBeenCalledTimes(2)
    expect(() => applyCompatibilityPatches(installed!.path)).not.toThrow()
    validateExtensionDirectory(installed!.path, TAG)
  })

  it('does not use the network for a cached resolution', async () => {
    const request = vi.fn()
    const installer = createPreviewUblockInstaller({ request, userDataPath: temporaryDirectory() })

    await expect(installer.resolve('cached')).resolves.toBeNull()
    expect(request).not.toHaveBeenCalled()
  })

  it('rejects a checksum mismatch and leaves no partial cache', async () => {
    const archive = archiveFiles()
    const release = releaseFor(archive)
    release.assets[0].digest = `sha256:${'0'.repeat(64)}`

    const request = vi
      .fn()
      .mockResolvedValueOnce(new TextEncoder().encode(JSON.stringify(release)))
      .mockResolvedValueOnce(archive)

    const cache = temporaryDirectory()
    const installer = createPreviewUblockInstaller({ request, userDataPath: cache })

    await expect(installer.resolve('latest')).rejects.toThrow(/checksum/i)
    expect(fs.existsSync(path.join(cache, 'preview-ublock', 'current'))).toBe(false)
  })

  it('rejects a non-canonical release asset URL before downloading', async () => {
    const archive = archiveFiles()
    const release = releaseFor(archive)
    release.assets[0].browser_download_url = 'https://example.com/uBOLite.zip'
    const request = vi.fn().mockResolvedValue(new TextEncoder().encode(JSON.stringify(release)))
    const installer = createPreviewUblockInstaller({ request, userDataPath: temporaryDirectory() })

    await expect(installer.resolve('latest')).rejects.toThrow(/canonical/i)
    expect(request).toHaveBeenCalledTimes(1)
    expect(request).toHaveBeenCalledWith(PREVIEW_UBLOCK_RELEASE_API, expect.any(Object))
  })

  it.each(['../escape.txt', '/absolute.txt', 'C:/drive.txt', 'nested\\escape.txt', 'nested/../escape.txt'])(
    'rejects unsafe ZIP path %s before writing files',
    unsafePath => {
      const staging = path.join(temporaryDirectory(), 'staging')
      const archive = zipSync({ [unsafePath]: new TextEncoder().encode('blocked') })

      expect(() => extractArchive(archive, staging, TAG)).toThrow(/unsafe path/i)
      expect(fs.existsSync(staging)).toBe(false)
    }
  )
})
