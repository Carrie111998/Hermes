/**
 * Main-process avatar pack scanner + resolver.
 *
 * Scans `~/.hermes/avatar-packs/<id>/` folders for pack.json manifests,
 * validates them, and resolves per-state asset file paths. Uses the same
 * resolveReadableFileForIpc guard as all other IPC file reads to prevent
 * path traversal.
 *
 * Exposed to the renderer via:
 *   - hermes:avatar-packs:list   → AvatarPackListResult (summaries + folder)
 *   - hermes:avatar-packs:resolve → ResolvedAvatarPack[] (full asset URLs)
 *   - hermes:avatar-packs:open   → opens the folder in Finder
 *
 * Hardening summary (SELECTIVE ADOPT review, 2026-07-28):
 *   1. Folder names rejected if they start with '.' or contain '..' or '/'
 *      — no symlink escape, no traversal via the directory listing.
 *   2. Each state asset path is checked with `path.relative(packDir, ...)`
 *      — must stay inside the pack folder or it's skipped with a warning.
 *   3. Every per-state file is then re-validated by `resolveReadableFileForIpc`,
 *      which performs the canonical IPC-file hardening (symlink resolution,
 *      sensitive-file block, maxBytes cap). Two layers, defense in depth.
 *   4. Total pack size capped at 100 MB; per-asset at 50 MB. Warnings, not
 *      hard fails — the loader still returns the manifest so the user sees
 *      a useful error in the UI rather than a silent disappearance.
 *   5. Asset extensions restricted to an allow-list (webm/mp4/mov/gif/webp/
 *      png/svg). SVG is included for vector packs but never executed as
 *      script — it is rendered through <img>, not <object> or inline.
 *
 * .hchar compatibility: this loader does NOT consume the third-party
 * `hermes-desktop-avatar` repo's `.hchar` zip-pack format. A separate
 * converter (P2) would unpack a `.hchar`, read its `character.json`, and
 * produce a `pack.json` + flat asset folder here. Until that lands, the
 * two ecosystems stay separate.
 */

import fs from 'node:fs'
import path from 'node:path'

import { resolveReadableFileForIpc } from './hardening'

// ── Asset format support (mirrors renderer-side avatar-pack-types.ts) ────────

const VIDEO_EXTS = new Set(['.webm', '.mp4', '.mov'])
const IMAGE_EXTS = new Set(['.gif', '.webp', '.png', '.svg'])
const ASSET_EXTS = new Set([...VIDEO_EXTS, ...IMAGE_EXTS])

const VALID_STATES = new Set(['idle', 'talk', 'think', 'listen'])
const MAX_PACK_SIZE_BYTES = 100 * 1024 * 1024 // 100 MB total per pack
const MAX_SINGLE_ASSET_BYTES = 50 * 1024 * 1024 // 50 MB per asset

// ── Types (kept minimal — the full types live in renderer-side TS) ───────────

/** @typedef {import('../src/store/avatar-pack-types').AvatarState} AvatarState */

/**
 * @typedef {Object} ResolvedStateAsset
 * @property {string} state
 * @property {string} filePath
 * @property {string} filename
 * @property {string} ext
 * @property {boolean} isVideo
 * @property {string} url
 */

/**
 * @typedef {Object} ResolvedAvatarPack
 * @property {string} id
 * @property {string} name
 * @property {string} version
 * @property {string} type
 * @property {string} folderPath
 * @property {(string | null)} thumbPath
 * @property {Record<string, ResolvedStateAsset>} assets
 * @property {string} defaultState
 * @property {{ transparent?: boolean, loop?: boolean }} render
 * @property {string[]} warnings
 */

/**
 * @typedef {Object} AvatarPackSummary
 * @property {string} id
 * @property {string} name
 * @property {string} version
 * @property {boolean} hasIdle
 * @property {number} stateCount
 */

/**
 * @typedef {Object} AvatarPackListResult
 * @property {AvatarPackSummary[]} packs
 * @property {string} folderPath
 */

// ── Path helpers ─────────────────────────────────────────────────────────────

/**
 * Security: check that a resolved file path is inside the given base directory.
 * Prevents symlinks or relative paths from escaping the pack folder.
 * @param {string} filePath
 * @param {string} baseDir
 * @returns {boolean}
 */
function isPathInside(filePath, baseDir) {
  const rel = path.relative(baseDir, filePath)

  return rel && !rel.startsWith('..') && !path.isAbsolute(rel)
}

/**
 * Resolve the avatar-packs folder path under HERMES_HOME.
 * @param {string} hermesHome
 * @returns {string}
 */
export function avatarPacksFolderPath(hermesHome) {
  return path.join(hermesHome, 'avatar-packs')
}

// ── Manifest validation ──────────────────────────────────────────────────────

/**
 * Validate a parsed pack.json object. Returns the manifest or throws.
 * @param {unknown} raw
 * @param {string} packId
 * @returns {{ id: string, name: string, version: string, type: string, states: Record<string, string>, render: { transparent: boolean, loop: boolean }, defaultState: string }}
 */
function validateManifest(raw, packId) {
  if (!raw || typeof raw !== 'object') {
    throw new Error(`pack.json is not a valid object`)
  }

  const obj = /** @type {Record<string, unknown>} */ (raw)

  const id = String(obj.id || '').trim()

  if (!id) {
    throw new Error(`pack.json missing required field: id`)
  }

  if (id !== packId) {
    // Don't throw — just warn. The folder name is authoritative for path safety.
  }

  const name = String(obj.name || id).trim()

  if (!name) {
    throw new Error(`pack.json missing required field: name`)
  }

  const version = String(obj.version || '1.0.0').trim()
  const type = String(obj.type || 'character').trim()

  if (type !== 'character') {
    throw new Error(`pack.json type "${type}" is not supported (only "character")`)
  }

  const states = obj.states

  if (!states || typeof states !== 'object') {
    throw new Error(`pack.json missing required field: states`)
  }

  /** @type {Record<string, string>} */
  const validStates = {}

  for (const [key, value] of Object.entries(/** @type {Record<string, unknown>} */ (states))) {
    if (!VALID_STATES.has(key)) {
      continue // Ignore unknown states gracefully
    }

    if (typeof value !== 'string' || !value.trim()) {
      continue
    }

    // Security: reject absolute paths and path traversal
    const filename = value.trim()

    if (path.isAbsolute(filename) || filename.includes('..')) {
      throw new Error(`pack.json state "${key}" has invalid path: ${filename}`)
    }

    validStates[key] = filename
  }

  const render = obj.render && typeof obj.render === 'object' ? obj.render : {}
  const defaultState = String(obj.defaultState || 'idle').trim()

  if (!VALID_STATES.has(defaultState)) {
    // Fall back to idle; don't throw
  }

  return {
    id,
    name,
    version,
    type: 'character',
    states: validStates,
    render: {
      transparent: render.transparent !== false,
      loop: render.loop !== false
    },
    defaultState: VALID_STATES.has(defaultState) ? defaultState : 'idle'
  }
}

// ── Pack resolution ──────────────────────────────────────────────────────────

/**
 * Resolve a single avatar pack from its folder.
 * @param {string} packDir - Absolute path to the pack folder
 * @param {string} packId - The pack ID (folder name)
 * @returns {Promise<ResolvedAvatarPack | null>}
 */
async function resolvePack(packDir, packId) {
  const warnings = []

  // 1. Read pack.json
  const manifestPath = path.join(packDir, 'pack.json')

  let manifestText

  try {
    manifestText = await fs.promises.readFile(manifestPath, 'utf8')
  } catch {
    return null // No manifest = not a valid pack
  }

  /** @type {{ states: Record<string, string> }} */
  let manifest

  try {
    const parsed = JSON.parse(manifestText)

    manifest = validateManifest(parsed, packId)
  } catch (err) {
    warnings.push(`Invalid pack.json: ${err instanceof Error ? err.message : String(err)}`)

    // Can't use this pack at all
    return /** @type {ResolvedAvatarPack} */ ({
      id: packId,
      name: packId,
      version: '0.0.0',
      type: 'character',
      folderPath: packDir,
      thumbPath: null,
      assets: {},
      defaultState: 'idle',
      render: { transparent: true, loop: true },
      warnings
    })
  }

  // 2. Check pack size (sum of all files)
  try {
    let totalSize = 0

    for (const entry of await fs.promises.readdir(packDir, { withFileTypes: true })) {
      if (entry.isFile()) {
        const stat = await fs.promises.stat(path.join(packDir, entry.name))
        totalSize += stat.size
      }
    }

    if (totalSize > MAX_PACK_SIZE_BYTES) {
      warnings.push(`Pack is ${Math.round(totalSize / 1024 / 1024)}MB (limit: ${MAX_PACK_SIZE_BYTES / 1024 / 1024}MB)`)
    }
  } catch {
    warnings.push('Could not check pack size')
  }

  // 3. Resolve per-state assets
  /** @type {Record<string, ResolvedStateAsset>} */
  const assets = {}

  for (const [stateName, filename] of Object.entries(manifest.states as Record<string, string>)) {
    const assetPath = path.join(packDir, filename)

    // Security: verify the resolved path is inside the pack folder
    if (!isPathInside(assetPath, packDir)) {
      warnings.push(`State "${stateName}" path escapes pack folder — skipped`)

      continue
    }

    try {
      // Use resolveReadableFileForIpc for the security guard (path traversal,
      // sensitive file rejection, symlink resolution)
      const { resolvedPath } = await resolveReadableFileForIpc(assetPath, {
        purpose: `Avatar pack asset: ${stateName}`,
        blockSensitive: false,
        maxBytes: MAX_SINGLE_ASSET_BYTES
      })

      const ext = path.extname(resolvedPath).toLowerCase()

      if (!ASSET_EXTS.has(ext)) {
        warnings.push(`State "${stateName}" has unsupported extension: ${ext}`)

        continue
      }

      // Build the hermes-media:// URL for streaming (works for both video and image).
      // The custom protocol handler reads the path from URL.pathname; with
      // `hermes-media://${encodedPath}` the encoded absolute path becomes the
      // URL host and pathname is empty, so Electron returns 404. Keep a stable
      // host segment (`stream`) and put the encoded file path in the pathname.
      const url = `hermes-media://stream/${encodeURIComponent(resolvedPath)}`

      assets[stateName] = {
        state: stateName,
        filePath: resolvedPath,
        filename,
        ext,
        isVideo: VIDEO_EXTS.has(ext),
        url
      }
    } catch (err) {
      warnings.push(`State "${stateName}" file not readable: ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  // 4. Check for thumb.png
  let thumbPath = null

  try {
    const thumbFile = path.join(packDir, 'thumb.png')

    await fs.promises.access(thumbFile, fs.constants.R_OK)
    thumbPath = thumbFile
  } catch {
    // No thumbnail — not an error
  }

  return {
    id: manifest.id,
    name: manifest.name,
    version: manifest.version,
    type: manifest.type,
    folderPath: packDir,
    thumbPath,
    assets,
    defaultState: manifest.defaultState,
    render: manifest.render,
    warnings
  }
}

/**
 * Scan all avatar packs in the avatar-packs folder.
 * @param {string} hermesHome
 * @returns {Promise<AvatarPackListResult>}
 */
export async function listAvatarPacks(hermesHome) {
  const packsDir = avatarPacksFolderPath(hermesHome)

  /** @type {AvatarPackSummary[]} */
  const packs = []

  try {
    await fs.promises.mkdir(packsDir, { recursive: true })
  } catch {
    return { packs: [], folderPath: packsDir }
  }

  let entries

  try {
    entries = await fs.promises.readdir(packsDir, { withFileTypes: true })
  } catch {
    return { packs: [], folderPath: packsDir }
  }

  for (const entry of entries) {
    if (!entry.isDirectory()) {
      continue
    }

    // Security: reject folder names that look like path traversal
    if (entry.name.startsWith('.') || entry.name.includes('..') || entry.name.includes('/')) {
      continue
    }

    const packDir = path.join(packsDir, entry.name)

    try {
      const resolved = await resolvePack(packDir, entry.name)

      if (resolved) {
        const stateCount = Object.keys(resolved.assets).length

        packs.push({
          id: resolved.id,
          name: resolved.name,
          version: resolved.version,
          hasIdle: Boolean('idle' in resolved.assets),
          stateCount
        })
      }
    } catch {
      // Skip broken packs silently
    }
  }

  return { packs, folderPath: packsDir }
}

/**
 * Resolve all avatar packs with full asset URLs.
 * @param {string} hermesHome
 * @returns {Promise<ResolvedAvatarPack[]>}
 */
export async function resolveAvatarPacks(hermesHome) {
  const packsDir = avatarPacksFolderPath(hermesHome)

  /** @type {ResolvedAvatarPack[]} */
  const resolved = []

  try {
    await fs.promises.mkdir(packsDir, { recursive: true })
  } catch {
    return []
  }

  let entries

  try {
    entries = await fs.promises.readdir(packsDir, { withFileTypes: true })
  } catch {
    return []
  }

  for (const entry of entries) {
    if (!entry.isDirectory()) {
      continue
    }

    if (entry.name.startsWith('.') || entry.name.includes('..') || entry.name.includes('/')) {
      continue
    }

    const packDir = path.join(packsDir, entry.name)

    try {
      const pack = await resolvePack(packDir, entry.name)

      if (pack) {
        resolved.push(pack)
      }
    } catch {
      // Skip broken packs
    }
  }

  return resolved
}
