import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import os from 'node:os'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { downloadArtifact } = require('@electron/get')
const { extract } = require('@electron-internal/extract-zip')
const electronPackage = require('electron/package.json')
const checksums = require('electron/checksums.json')

const platform = process.env.npm_config_platform || process.platform
const arch = process.env.npm_config_arch || process.arch
const archivePath = await downloadArtifact({
  version: electronPackage.version,
  artifactName: 'electron',
  force: false,
  cacheRoot: process.env.electron_config_cache,
  checksums,
  platform,
  arch
})

const destination = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-electron-extraction-'))

function executablePath(root) {
  switch (platform) {
    case 'darwin':
    case 'mas':
      return path.join(root, 'Electron.app', 'Contents', 'MacOS', 'Electron')
    case 'freebsd':
    case 'openbsd':
    case 'linux':
      return path.join(root, 'electron')
    case 'win32':
      return path.join(root, 'electron.exe')
    default:
      throw new Error(`Unsupported Electron verification platform: ${platform}`)
  }
}

function countExtractedFiles(root) {
  const pending = [root]
  let count = 0
  while (pending.length > 0) {
    const directory = pending.pop()
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const entryPath = path.join(directory, entry.name)
      if (entry.isDirectory()) pending.push(entryPath)
      else count += 1
    }
  }
  return count
}

try {
  await extract(archivePath, { dir: destination })
  const executable = executablePath(destination)
  fs.accessSync(executable, fs.constants.R_OK)

  const fileCount = countExtractedFiles(destination)
  assert.ok(
    fileCount >= 100,
    `Electron archive extraction was suspiciously incomplete: ${fileCount} files`
  )

  console.log(`Electron archive: ${archivePath}`)
  console.log(`Electron archive executable: ${executable}`)
  console.log(`Electron archive extracted files: ${fileCount}`)
} finally {
  fs.rmSync(destination, { recursive: true, force: true })
}
