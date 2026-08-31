import crypto from 'node:crypto'
import fs from 'node:fs'
import https from 'node:https'
import path from 'node:path'

import { unzipSync } from 'fflate'

export type UblockInstallIntent = 'cached' | 'latest'

export interface InstalledUblock {
  path: string
  version: string
}

export interface PreviewUblockInstaller {
  resolve(intent: UblockInstallIntent): Promise<InstalledUblock | null>
}

export interface UblockRelease {
  archiveSha256: string
  archiveUrl: string
  tag: string
}

export interface PreviewUblockHttpRequestOptions {
  headers?: Record<string, string>
  maxBytes: number
  maxRedirects: number
  timeoutMs: number
}

export type PreviewUblockHttpRequest = (url: string, options: PreviewUblockHttpRequestOptions) => Promise<Uint8Array>

export interface PreviewUblockInstallerOptions {
  cacheDirectory?: string
  request?: PreviewUblockHttpRequest
  userDataPath?: string
}

export interface InstalledUblockMetadata {
  archiveSha256: string
  archiveUrl: string
  schemaVersion: 1
  version: string
}

export const PREVIEW_UBLOCK_RELEASE_API = 'https://api.github.com/repos/uBlockOrigin/uBOL-home/releases/latest'
export const PREVIEW_UBLOCK_MAX_ARCHIVE_BYTES = 80 * 1024 * 1024
export const PREVIEW_UBLOCK_MAX_EXPANDED_BYTES = 128 * 1024 * 1024
export const PREVIEW_UBLOCK_MAX_FILE_BYTES = 32 * 1024 * 1024
export const PREVIEW_UBLOCK_MAX_FILES = 2_000

const PREVIEW_UBLOCK_CACHE_DIRNAME = 'preview-ublock'
const INSTALLED_METADATA_FILENAME = 'installed.json'
const CURRENT_DIRNAME = 'current'
const MAX_RELEASE_METADATA_BYTES = 1024 * 1024
const DOWNLOAD_TIMEOUT_MS = 30_000
const MAX_REDIRECTS = 5
const RELEASE_TAG_RE = /^[A-Za-z0-9._-]+$/
const SHA256_RE = /^[0-9a-f]{64}$/

const ALLOWED_NETWORK_HOSTS = new Set([
  'api.github.com',
  'github.com',
  'release-assets.githubusercontent.com',
  'objects.githubusercontent.com'
])

const REQUIRED_FILES = [
  'LICENSE.txt',
  'manifest.json',
  'dashboard.html',
  'rulesets/main/easylist.json',
  'rulesets/main/easyprivacy.json',
  'rulesets/main/ublock-filters.json'
]

const PATCHES: Array<{ file: string; oldText: string; newText: string; marker: string }> = [
  {
    file: 'js/background.js',
    oldText: `browser.permissions.onRemoved.addListener((...args) => {
        isFullyInitialized.then(( ) => {
            onPermissionsChanged('removed', ...args);
        });
    });`,
    newText: `if ( browser.permissions?.onRemoved?.addListener ) {
    browser.permissions.onRemoved.addListener((...args) => {
        isFullyInitialized.then(( ) => {
            onPermissionsChanged('removed', ...args);
        });
    });
}`,
    marker: 'browser.permissions?.onRemoved?.addListener'
  },
  {
    file: 'js/background.js',
    oldText: `browser.permissions.onAdded.addListener((...args) => {
        isFullyInitialized.then(( ) => {
            onPermissionsChanged('added', ...args);
        });
    });`,
    newText: `if ( browser.permissions?.onAdded?.addListener ) {
    browser.permissions.onAdded.addListener((...args) => {
        isFullyInitialized.then(( ) => {
            onPermissionsChanged('added', ...args);
        });
    });
}`,
    marker: 'browser.permissions?.onAdded?.addListener'
  },
  {
    file: 'js/background.js',
    oldText: `browser.commands.onCommand.addListener((...args) => {
        isFullyInitialized.then(( ) => {
            onCommand(...args);
        });
    });`,
    newText: `if ( browser.commands?.onCommand?.addListener ) {
    browser.commands.onCommand.addListener((...args) => {
        isFullyInitialized.then(( ) => {
            onCommand(...args);
        });
    });
}`,
    marker: 'browser.commands?.onCommand?.addListener'
  },
  {
    file: 'js/ext-utils.js',
    oldText: `export async function hasBroadHostPermissions() {
    return browser.permissions.getAll().then(permissions =>`,
    newText: `export async function hasBroadHostPermissions() {
    if ( browser.permissions?.getAll === undefined ) { return true; }
    return browser.permissions.getAll().then(permissions =>`,
    marker: 'browser.permissions?.getAll === undefined'
  },
  {
    file: 'js/mode-manager.js',
    oldText: `async function getBrowserPermissions() {
    return browser.permissions.getAll();
}`,
    newText: `async function getBrowserPermissions() {
    if ( browser.permissions?.getAll === undefined ) {
        return { origins: [ '<all_urls>' ] };
    }
    return browser.permissions.getAll();
}`,
    marker: 'browser.permissions?.getAll === undefined'
  },
  {
    file: 'js/mode-manager.js',
    oldText: `        const permissions = await browser.permissions.getAll();
            iter = hostnamesFromMatches(permissions.origins) || [];`,
    newText: `        if ( browser.permissions?.getAll === undefined ) {
            iter = [ 'all-urls' ];
        } else {
            const permissions = await browser.permissions.getAll();
            iter = hostnamesFromMatches(permissions.origins) || [];
        }`,
    marker: "iter = [ 'all-urls' ]"
  }
]

function safeError(message: string): Error {
  return new Error(`uBlock Origin Lite could not be installed: ${message}`)
}

function canonicalArchiveUrl(tag: string): string {
  return `https://github.com/uBlockOrigin/uBOL-home/releases/download/${tag}/uBOLite_${tag}.chromium.zip`
}

function isAllowedUrl(rawUrl: string): URL {
  let url: URL

  try {
    url = new URL(rawUrl)
  } catch {
    throw safeError('the release server returned an invalid URL')
  }

  if (url.protocol !== 'https:' || url.username || url.password || !ALLOWED_NETWORK_HOSTS.has(url.hostname)) {
    throw safeError('the release server returned a disallowed URL')
  }

  return url
}

function requestHttps(urlString: string, options: PreviewUblockHttpRequestOptions): Promise<Uint8Array> {
  const visit = (currentUrl: string, redirects: number): Promise<Uint8Array> =>
    new Promise((resolve, reject) => {
      const url = isAllowedUrl(currentUrl)

      const request = https.request(url, { headers: options.headers, method: 'GET' }, response => {
        const status = response.statusCode ?? 0

        if (status >= 300 && status < 400) {
          const location = response.headers.location
          response.resume()

          if (!location || redirects >= options.maxRedirects) {
            reject(safeError('the release download redirected too many times'))

            return
          }

          let nextUrl: URL

          try {
            nextUrl = new URL(location, url)
            isAllowedUrl(nextUrl.toString())
          } catch (error) {
            reject(error instanceof Error ? error : safeError('the release download returned an invalid redirect'))

            return
          }

          void visit(nextUrl.toString(), redirects + 1).then(resolve, reject)

          return
        }

        if (status < 200 || status >= 300) {
          response.resume()
          reject(safeError(`the release server returned HTTP ${status}`))

          return
        }

        const chunks: Buffer[] = []
        let total = 0
        let tooLarge = false
        response.on('data', chunk => {
          if (tooLarge) {
            return
          }
          const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)
          total += buffer.length

          if (total > options.maxBytes) {
            tooLarge = true
            response.destroy(safeError('the release response is too large'))

            return
          }

          chunks.push(buffer)
        })
        response.on('end', () => {
          if (!tooLarge) {
            resolve(new Uint8Array(Buffer.concat(chunks)))
          }
        })
        response.on('error', reject)
      })

      request.setTimeout(options.timeoutMs, () => request.destroy(safeError('the release request timed out')))
      request.on('error', reject)
      request.end()
    })

  return visit(urlString, 0)
}

function countLineOccurrences(source: string, target: string): number {
  let count = 0
  let offset = 0

  while (true) {
    const index = source.indexOf(target, offset)

    if (index < 0) {
      return count
    }
    if (index === 0 || source[index - 1] === '\n') {
      count += 1
    }
    offset = index + target.length
  }
}

function replaceLineOccurrence(source: string, target: string, replacement: string): string {
  let offset = 0

  while (true) {
    const index = source.indexOf(target, offset)

    if (index < 0) {
      return source
    }
    if (index === 0 || source[index - 1] === '\n') {
      return `${source.slice(0, index)}${replacement}${source.slice(index + target.length)}`
    }
    offset = index + target.length
  }
}

function applyCompatibilityPatches(root: string): void {
  for (const patch of PATCHES) {
    const filePath = path.join(root, patch.file)
    let source: string

    try {
      source = fs.readFileSync(filePath, 'utf8')
    } catch {
      throw safeError(`the required compatibility file is missing: ${patch.file}`)
    }

    const oldCount = countLineOccurrences(source, patch.oldText)
    const newCount = countLineOccurrences(source, patch.newText)

    if (oldCount === 1 && newCount === 0) {
      fs.writeFileSync(filePath, replaceLineOccurrence(source, patch.oldText, patch.newText), {
        encoding: 'utf8',
        mode: 0o600
      })
    } else if (oldCount !== 0 || newCount !== 1) {
      throw safeError(`compatibility patch context drifted in ${patch.file}`)
    }
  }
}

function decodeZipName(bytes: Uint8Array): string {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    throw safeError('the archive contains an invalid UTF-8 path')
  }
}

function checkedZipPath(name: string): string {
  if (!name || name.includes('\\') || name.includes('\0') || name.startsWith('/') || /^[A-Za-z]:/.test(name)) {
    throw safeError('the archive contains an unsafe path')
  }

  const pathWithoutMarker = name.endsWith('/') ? name.slice(0, -1) : name
  const segments = pathWithoutMarker.split('/')

  if (!pathWithoutMarker || segments.some(segment => segment === '' || segment === '.' || segment === '..')) {
    throw safeError('the archive contains an unsafe path')
  }

  return segments.join('/')
}

function readU16(data: Uint8Array, offset: number): number {
  if (offset < 0 || offset + 2 > data.length) {
    throw safeError('the archive is truncated')
  }

  return data[offset] | (data[offset + 1] << 8)
}

function readU32(data: Uint8Array, offset: number): number {
  if (offset < 0 || offset + 4 > data.length) {
    throw safeError('the archive is truncated')
  }

  return (data[offset] | (data[offset + 1] << 8) | (data[offset + 2] << 16) | (data[offset + 3] << 24)) >>> 0
}

interface ZipEntry {
  compressedSize: number
  isDirectory: boolean
  name: string
  uncompressedSize: number
}

function inspectZip(archive: Uint8Array): ZipEntry[] {
  if (archive.length > PREVIEW_UBLOCK_MAX_ARCHIVE_BYTES) {
    throw safeError('the archive is too large')
  }

  const minimumEnd = Math.max(0, archive.length - 22 - 65_535)
  let endOffset = -1

  for (let offset = archive.length - 22; offset >= minimumEnd; offset -= 1) {
    if (offset >= 0 && readU32(archive, offset) === 0x06054b50) {
      endOffset = offset

      break
    }
  }

  if (endOffset < 0) {
    throw safeError('the archive has no valid ZIP directory')
  }

  const diskNumber = readU16(archive, endOffset + 4)
  const directoryDisk = readU16(archive, endOffset + 6)
  const entriesOnDisk = readU16(archive, endOffset + 8)
  const entryCount = readU16(archive, endOffset + 10)
  const directorySize = readU32(archive, endOffset + 12)
  const directoryOffset = readU32(archive, endOffset + 16)

  if (diskNumber !== 0 || directoryDisk !== 0 || entriesOnDisk !== entryCount || entryCount === 0xffff) {
    throw safeError('the archive uses unsupported ZIP features')
  }

  if (directoryOffset + directorySize > archive.length) {
    throw safeError('the archive directory is truncated')
  }

  const entries: ZipEntry[] = []
  const names = new Set<string>()
  let offset = directoryOffset
  let expandedBytes = 0
  let fileCount = 0

  for (let index = 0; index < entryCount; index += 1) {
    if (readU32(archive, offset) !== 0x02014b50) {
      throw safeError('the archive has an invalid directory entry')
    }
    const flags = readU16(archive, offset + 8)
    const compression = readU16(archive, offset + 10)
    const compressedSize = readU32(archive, offset + 20)
    const uncompressedSize = readU32(archive, offset + 24)
    const nameLength = readU16(archive, offset + 28)
    const extraLength = readU16(archive, offset + 30)
    const commentLength = readU16(archive, offset + 32)
    const externalAttributes = readU32(archive, offset + 38)
    const localOffset = readU32(archive, offset + 42)
    const nameStart = offset + 46
    const nextOffset = nameStart + nameLength + extraLength + commentLength

    if (nextOffset > directoryOffset + directorySize || nextOffset > archive.length) {
      throw safeError('the archive is truncated')
    }

    if (compressedSize === 0xffffffff || uncompressedSize === 0xffffffff || localOffset === 0xffffffff) {
      throw safeError('the archive uses unsupported ZIP64 features')
    }

    const rawName = decodeZipName(archive.slice(nameStart, nameStart + nameLength))
    const name = checkedZipPath(rawName)

    if (names.has(name)) {
      throw safeError('the archive contains duplicate paths')
    }
    names.add(name)

    const unixMode = (externalAttributes >>> 16) & 0xffff

    if ((unixMode & 0xf000) === 0xa000) {
      throw safeError('the archive contains a symlink')
    }

    if (flags & 1) {
      throw safeError('the archive contains an encrypted entry')
    }

    if (compression !== 0 && compression !== 8) {
      throw safeError('the archive uses unsupported compression')
    }

    if (readU32(archive, localOffset) !== 0x04034b50) {
      throw safeError('the archive has an invalid local entry')
    }

    const localNameLength = readU16(archive, localOffset + 26)
    const localExtraLength = readU16(archive, localOffset + 28)
    const dataOffset = localOffset + 30 + localNameLength + localExtraLength

    if (dataOffset > archive.length || compressedSize > archive.length - dataOffset) {
      throw safeError('the archive entry is truncated')
    }

    const isDirectory = rawName.endsWith('/') || (externalAttributes & 0x10) !== 0 || (unixMode & 0xf000) === 0x4000

    if (!isDirectory) {
      fileCount += 1

      if (fileCount > PREVIEW_UBLOCK_MAX_FILES) {
        throw safeError('the archive contains too many files')
      }

      if (uncompressedSize > PREVIEW_UBLOCK_MAX_FILE_BYTES) {
        throw safeError('an archive file is too large')
      }
      expandedBytes += uncompressedSize

      if (expandedBytes > PREVIEW_UBLOCK_MAX_EXPANDED_BYTES) {
        throw safeError('the expanded archive is too large')
      }
    }

    entries.push({ compressedSize, isDirectory, name, uncompressedSize })
    offset = nextOffset
  }

  return entries
}

function ensureInside(root: string, candidate: string): void {
  const resolvedRoot = path.resolve(root)
  const resolvedCandidate = path.resolve(candidate)

  if (resolvedCandidate !== resolvedRoot && !resolvedCandidate.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw safeError('the archive escaped its staging directory')
  }
}

function extractArchive(archive: Uint8Array, stagingPath: string, tag: string): void {
  const entries = inspectZip(archive)
  const files = unzipSync(archive)
  let actualExpandedBytes = 0
  let actualFileCount = 0
  fs.mkdirSync(stagingPath, { recursive: true, mode: 0o700 })
  fs.chmodSync(stagingPath, 0o700)

  for (const entry of entries) {
    const destination = path.join(stagingPath, entry.name)
    ensureInside(stagingPath, destination)

    if (entry.isDirectory) {
      fs.mkdirSync(destination, { recursive: true, mode: 0o700 })
      fs.chmodSync(destination, 0o700)

      continue
    }

    const data = files[entry.name]
    actualFileCount += 1
    actualExpandedBytes += data?.length ?? 0
    if (actualFileCount > PREVIEW_UBLOCK_MAX_FILES || actualExpandedBytes > PREVIEW_UBLOCK_MAX_EXPANDED_BYTES) {
      throw safeError('the expanded archive is too large')
    }

    if (!data || data.length !== entry.uncompressedSize) {
      throw safeError('the archive contents do not match its directory')
    }
    if (data.length > PREVIEW_UBLOCK_MAX_FILE_BYTES) {
      throw safeError('an archive file is too large')
    }

    fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 })
    fs.writeFileSync(destination, Buffer.from(data), { encoding: null, mode: 0o600 })
    fs.chmodSync(destination, 0o600)
  }

  validateExtensionDirectory(stagingPath, tag, false)
}

function safeManifestPath(value: unknown, fallback: string): string {
  if (value === undefined) {
    return fallback
  }

  if (typeof value !== 'string' || value.includes('\\')) {
    throw safeError('the manifest contains an unsafe path')
  }

  return checkedZipPath(value.replace(/^\/+/, ''))
}

function readManifest(root: string): Record<string, any> {
  try {
    const value: unknown = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json'), 'utf8'))

    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('not an object')
    }

    return value as Record<string, any>
  } catch {
    throw safeError('manifest.json is invalid')
  }
}

function validateExtensionTree(root: string): void {
  const rootStat = fs.lstatSync(root)

  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
    throw safeError('the extension cache root is not a directory')
  }

  const visit = (directory: string): void => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const entryPath = path.join(directory, entry.name)
      ensureInside(root, entryPath)
      const stat = fs.lstatSync(entryPath)

      if (stat.isSymbolicLink()) {
        throw safeError('the extension cache contains a symlink')
      }

      if (stat.isDirectory()) {
        visit(entryPath)
      } else if (!stat.isFile()) {
        throw safeError('the extension cache contains a non-regular file')
      }
    }
  }

  visit(root)
}

function validateCompatibilityMarkers(root: string): void {
  for (const patch of PATCHES) {
    const source = fs.readFileSync(path.join(root, patch.file), 'utf8')

    if (source.includes(patch.marker) === false) {
      throw safeError(`compatibility patch is missing from ${patch.file}`)
    }
  }
}

export function validateExtensionDirectory(root: string, tag: string, requireCompatibilityPatches = true): void {
  if (!RELEASE_TAG_RE.test(tag)) {
    throw safeError('the release tag is invalid')
  }
  validateExtensionTree(root)
  const manifest = readManifest(root)

  if (manifest.manifest_version !== 3 || typeof manifest.version !== 'string' || manifest.version !== tag) {
    throw safeError('the extension manifest is not the requested MV3 release')
  }

  if (typeof manifest.name !== 'string' || manifest.name.trim() === '') {
    throw safeError('the extension name is missing')
  }

  const dashboard = safeManifestPath(manifest.dashboard, 'dashboard.html')
  const serviceWorker = safeManifestPath(manifest.background?.service_worker, '')

  if (!serviceWorker) {
    throw safeError('the background service worker is missing')
  }

  const required = new Set([...REQUIRED_FILES, dashboard, serviceWorker])

  for (const relativePath of required) {
    const absolutePath = path.join(root, relativePath)
    ensureInside(root, absolutePath)
    let stat: fs.Stats

    try {
      stat = fs.lstatSync(absolutePath)
    } catch {
      throw safeError(`required extension file is missing: ${relativePath}`)
    }

    if (!stat.isFile() || stat.isSymbolicLink()) {
      throw safeError(`required extension file is not regular: ${relativePath}`)
    }
  }

  if (requireCompatibilityPatches) {
    validateCompatibilityMarkers(root)
  }
}

function validateMetadata(value: unknown): value is InstalledUblockMetadata {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }
  const candidate = value as Record<string, unknown>
  const keys = Object.keys(candidate).sort()

  return (
    keys.join(',') === 'archiveSha256,archiveUrl,schemaVersion,version' &&
    candidate.schemaVersion === 1 &&
    typeof candidate.version === 'string' &&
    RELEASE_TAG_RE.test(candidate.version) &&
    typeof candidate.archiveSha256 === 'string' &&
    SHA256_RE.test(candidate.archiveSha256) &&
    typeof candidate.archiveUrl === 'string' &&
    candidate.archiveUrl === canonicalArchiveUrl(candidate.version)
  )
}

function metadataPathFor(cacheDirectory: string): string {
  return path.join(cacheDirectory, INSTALLED_METADATA_FILENAME)
}

function readMetadata(cacheDirectory: string): InstalledUblockMetadata | null {
  try {
    const value: unknown = JSON.parse(fs.readFileSync(metadataPathFor(cacheDirectory), 'utf8'))

    return validateMetadata(value) ? value : null
  } catch {
    return null
  }
}

function validateCachedInstall(cacheDirectory: string): InstalledUblockMetadata | null {
  const metadata = readMetadata(cacheDirectory)
  const currentPath = path.join(cacheDirectory, CURRENT_DIRNAME)

  if (!metadata) {
    return null
  }

  try {
    if (!fs.statSync(currentPath).isDirectory()) {
      return null
    }
    validateExtensionDirectory(currentPath, metadata.version)

    return metadata
  } catch {
    return null
  }
}

function removePath(target: string): void {
  fs.rmSync(target, { force: true, recursive: true })
}

function cacheEntries(cacheDirectory: string, prefix: string): string[] {
  try {
    return fs
      .readdirSync(cacheDirectory)
      .filter(entry => entry.startsWith(prefix))
      .map(entry => path.join(cacheDirectory, entry))
  } catch {
    return []
  }
}

function validateCachedInstallAt(currentPath: string, cacheDirectory: string): boolean {
  const metadata = readMetadata(cacheDirectory)

  if (!metadata) {
    return false
  }

  try {
    validateExtensionDirectory(currentPath, metadata.version)

    return true
  } catch {
    return false
  }
}

function recoverCache(cacheDirectory: string): void {
  fs.mkdirSync(cacheDirectory, { recursive: true, mode: 0o700 })

  for (const stagingPath of cacheEntries(cacheDirectory, '.staging-')) {
    removePath(stagingPath)
  }

  for (const temporaryMetadataPath of cacheEntries(cacheDirectory, '.installed-')) {
    removePath(temporaryMetadataPath)
  }

  const currentPath = path.join(cacheDirectory, CURRENT_DIRNAME)
  const previousPaths = cacheEntries(cacheDirectory, '.previous-')

  if (!fs.existsSync(currentPath)) {
    for (const previousPath of previousPaths) {
      if (validateCachedInstallAt(previousPath, cacheDirectory)) {
        fs.renameSync(previousPath, currentPath)

        break
      }
    }
  }

  for (const previousPath of cacheEntries(cacheDirectory, '.previous-')) {
    removePath(previousPath)
  }
}

function randomSuffix(): string {
  return `${process.pid}-${crypto.randomBytes(8).toString('hex')}`
}

function promoteCache(cacheDirectory: string, stagingPath: string, metadata: InstalledUblockMetadata): void {
  const currentPath = path.join(cacheDirectory, CURRENT_DIRNAME)
  const previousPath = path.join(cacheDirectory, `.previous-${randomSuffix()}`)
  const metadataPath = metadataPathFor(cacheDirectory)
  const temporaryMetadataPath = path.join(cacheDirectory, `.installed-${randomSuffix()}.json`)
  const oldMetadata = fs.existsSync(metadataPath) ? fs.readFileSync(metadataPath) : null
  let previousMoved = false
  let currentPromoted = false

  try {
    if (fs.existsSync(currentPath)) {
      fs.renameSync(currentPath, previousPath)
      previousMoved = true
    }

    fs.renameSync(stagingPath, currentPath)
    currentPromoted = true
    fs.writeFileSync(temporaryMetadataPath, JSON.stringify(metadata, null, 2), { encoding: 'utf8', mode: 0o600 })
    fs.renameSync(temporaryMetadataPath, metadataPath)

    if (previousMoved) {
      removePath(previousPath)
    }
  } catch (error) {
    removePath(temporaryMetadataPath)

    if (currentPromoted) {
      removePath(currentPath)
    }

    if (previousMoved && fs.existsSync(previousPath)) {
      fs.renameSync(previousPath, currentPath)
    }

    if (oldMetadata) {
      fs.writeFileSync(metadataPath, oldMetadata, { mode: 0o600 })
    } else {
      removePath(metadataPath)
    }

    throw error
  }
}

function parseRelease(payload: Uint8Array): UblockRelease {
  let value: any

  try {
    value = JSON.parse(Buffer.from(payload).toString('utf8'))
  } catch {
    throw safeError('the release metadata is invalid JSON')
  }

  if (
    !value ||
    value.draft !== false ||
    value.prerelease !== false ||
    typeof value.tag_name !== 'string' ||
    !RELEASE_TAG_RE.test(value.tag_name)
  ) {
    throw safeError('the release metadata is not a stable release')
  }

  const tag = value.tag_name
  const assetName = `uBOLite_${tag}.chromium.zip`
  const assets = Array.isArray(value.assets) ? value.assets.filter((asset: any) => asset?.name === assetName) : []

  if (assets.length !== 1) {
    throw safeError('the stable release has no unique uBlock archive')
  }
  const asset = assets[0]

  if (
    asset.state !== 'uploaded' ||
    asset.content_type !== 'application/zip' ||
    !Number.isInteger(asset.size) ||
    asset.size < 1 ||
    asset.size > PREVIEW_UBLOCK_MAX_ARCHIVE_BYTES ||
    typeof asset.digest !== 'string' ||
    !/^sha256:[0-9a-f]{64}$/.test(asset.digest)
  ) {
    throw safeError('the stable release archive metadata is invalid')
  }

  const archiveUrl = canonicalArchiveUrl(tag)

  if (asset.browser_download_url !== archiveUrl) {
    throw safeError('the release archive URL is not canonical')
  }

  return { archiveSha256: asset.digest.slice('sha256:'.length), archiveUrl, tag }
}

export function createPreviewUblockInstaller({
  cacheDirectory,
  request = requestHttps,
  userDataPath
}: PreviewUblockInstallerOptions): PreviewUblockInstaller {
  const resolvedCacheDirectory =
    cacheDirectory ?? (userDataPath ? path.join(userDataPath, PREVIEW_UBLOCK_CACHE_DIRNAME) : null)

  if (!resolvedCacheDirectory) {
    throw new Error('uBlock Origin Lite cache directory is not configured')
  }
  fs.mkdirSync(resolvedCacheDirectory, { recursive: true, mode: 0o700 })
  recoverCache(resolvedCacheDirectory)

  let latestPromise: Promise<InstalledUblock> | null = null

  const resolveCached = (): InstalledUblock | null => {
    const metadata = validateCachedInstall(resolvedCacheDirectory)

    if (!metadata) {
      removePath(path.join(resolvedCacheDirectory, CURRENT_DIRNAME))
      removePath(metadataPathFor(resolvedCacheDirectory))

      return null
    }

    return { path: path.join(resolvedCacheDirectory, CURRENT_DIRNAME), version: metadata.version }
  }

  const resolveLatest = async (): Promise<InstalledUblock> => {
    const releasePayload = await request(PREVIEW_UBLOCK_RELEASE_API, {
      headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'Hermes-Desktop' },
      maxBytes: MAX_RELEASE_METADATA_BYTES,
      maxRedirects: MAX_REDIRECTS,
      timeoutMs: DOWNLOAD_TIMEOUT_MS
    })

    const release = parseRelease(releasePayload)
    const cachedMetadata = validateCachedInstall(resolvedCacheDirectory)

    if (
      cachedMetadata &&
      cachedMetadata.version === release.tag &&
      cachedMetadata.archiveSha256 === release.archiveSha256 &&
      cachedMetadata.archiveUrl === release.archiveUrl
    ) {
      return { path: path.join(resolvedCacheDirectory, CURRENT_DIRNAME), version: cachedMetadata.version }
    }

    if (!cachedMetadata) {
      removePath(path.join(resolvedCacheDirectory, CURRENT_DIRNAME))
      removePath(metadataPathFor(resolvedCacheDirectory))
    }

    const archive = await request(release.archiveUrl, {
      maxBytes: PREVIEW_UBLOCK_MAX_ARCHIVE_BYTES,
      maxRedirects: MAX_REDIRECTS,
      timeoutMs: DOWNLOAD_TIMEOUT_MS
    })

    const actualDigest = crypto.createHash('sha256').update(archive).digest()
    const expectedDigest = Buffer.from(release.archiveSha256, 'hex')

    if (actualDigest.length !== expectedDigest.length || !crypto.timingSafeEqual(actualDigest, expectedDigest)) {
      throw safeError('the downloaded archive checksum did not match GitHub')
    }

    const stagingPath = path.join(resolvedCacheDirectory, `.staging-${randomSuffix()}`)

    try {
      extractArchive(archive, stagingPath, release.tag)
      applyCompatibilityPatches(stagingPath)
      validateExtensionDirectory(stagingPath, release.tag)

      const metadata: InstalledUblockMetadata = {
        archiveSha256: release.archiveSha256,
        archiveUrl: release.archiveUrl,
        schemaVersion: 1,
        version: release.tag
      }

      promoteCache(resolvedCacheDirectory, stagingPath, metadata)
    } catch (error) {
      removePath(stagingPath)
      throw error
    }

    return { path: path.join(resolvedCacheDirectory, CURRENT_DIRNAME), version: release.tag }
  }

  return {
    resolve(intent) {
      if (intent === 'cached') {
        return Promise.resolve(resolveCached())
      }

      if (!latestPromise) {
        latestPromise = resolveLatest().finally(() => {
          latestPromise = null
        })
      }

      return latestPromise
    }
  }
}

export { applyCompatibilityPatches, canonicalArchiveUrl, extractArchive, parseRelease }
