#!/usr/bin/env node

import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { randomUUID } from 'node:crypto'

import {
  getManagedReleaseAssetNames,
  hashFile,
  parseManagedUpdateInfo,
  verifyManagedUpdateRelease
} from './verify-managed-update-release.mjs'
import { isMain } from './utils.mjs'

const DEFAULT_ARCH = 'arm64'
const packageJson = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
const require = createRequire(import.meta.url)

function unquoteYamlScalar(value) {
  const trimmed = String(value || '').trim()
  if (
    trimmed.length >= 2 &&
    ((trimmed.startsWith("'") && trimmed.endsWith("'")) || (trimmed.startsWith('"') && trimmed.endsWith('"')))
  ) {
    return trimmed.slice(1, -1)
  }

  return trimmed
}

function electronBuilderBlockMap(inFile, compressionFormat, outFile) {
  const { buildBlockMap } = require('app-builder-lib/out/targets/blockmap/blockmap.js')

  return buildBlockMap(inFile, compressionFormat, outFile)
}

function oneFileRecord(updateInfo, name) {
  const records = updateInfo.files.filter(record => record.url === name)
  if (records.length !== 1) {
    throw new Error(`latest-mac.yml must contain exactly one file record for ${name}.`)
  }

  return records[0]
}

export function replaceManagedUpdateFileRecord(source, { name, sha512, size }) {
  const newline = String(source).includes('\r\n') ? '\r\n' : '\n'
  const lines = String(source).split(/\r?\n/)
  let inFiles = false
  let currentUrl = null
  let matchingRecords = 0
  let shaUpdates = 0
  let sizeUpdates = 0

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]
    const topLevel = line.match(/^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$/)
    if (topLevel) {
      inFiles = topLevel[1] === 'files'
      currentUrl = null
      continue
    }
    if (!inFiles) {
      continue
    }

    const url = line.match(/^(\s*)-\s+url:\s*(.+)$/)
    if (url) {
      currentUrl = unquoteYamlScalar(url[2])
      if (currentUrl === name) {
        matchingRecords += 1
      }
      continue
    }

    if (currentUrl !== name) {
      continue
    }

    const field = line.match(/^(\s+)(sha512|size):\s*(.*)$/)
    if (!field) {
      continue
    }
    if (field[2] === 'sha512') {
      lines[index] = `${field[1]}sha512: ${sha512}`
      shaUpdates += 1
    }
    if (field[2] === 'size') {
      lines[index] = `${field[1]}size: ${size}`
      sizeUpdates += 1
    }
  }

  if (matchingRecords !== 1 || shaUpdates !== 1 || sizeUpdates !== 1) {
    throw new Error(`Could not update the unique complete latest-mac.yml record for ${name}.`)
  }

  return lines.join(newline)
}

export async function refreshManagedUpdateMetadata(options = {}) {
  const manifest = options.manifest ?? packageJson
  const releaseDir = path.resolve(options.releaseDir ?? 'release')
  const version = String(options.version || manifest.version || '').trim()
  const arch = String(options.arch || DEFAULT_ARCH).trim()
  const buildBlockMap = options.buildBlockMap ?? electronBuilderBlockMap

  if (!version) {
    throw new Error('Expected an evaOS Agent version.')
  }
  if (arch !== DEFAULT_ARCH) {
    throw new Error(`Managed macOS release metadata refresh is arm64-only; received ${arch}.`)
  }

  const names = getManagedReleaseAssetNames(manifest, { arch, version })
  const paths = {
    dmg: path.join(releaseDir, names.dmg),
    dmgBlockmap: path.join(releaseDir, names.dmgBlockmap),
    updateInfo: path.join(releaseDir, names.updateInfo),
    zip: path.join(releaseDir, names.zip)
  }
  for (const filePath of Object.values(paths)) {
    if (!fs.statSync(filePath, { throwIfNoEntry: false })?.isFile()) {
      throw new Error(`Missing required signed-update asset: ${filePath}`)
    }
  }

  const originalSource = fs.readFileSync(paths.updateInfo, 'utf8')
  const originalInfo = parseManagedUpdateInfo(originalSource)
  if (originalInfo.version !== version) {
    throw new Error(`latest-mac.yml does not identify exact version ${version}.`)
  }

  const originalZip = oneFileRecord(originalInfo, names.zip)
  oneFileRecord(originalInfo, names.dmg)
  if (originalInfo.path !== names.zip || originalInfo.sha512 !== originalZip.sha512) {
    throw new Error('latest-mac.yml primary updater path and SHA-512 must identify the ZIP payload.')
  }

  const zipAsset = await hashFile(paths.zip)
  if (originalZip.size !== zipAsset.bytes || originalZip.sha512 !== zipAsset.sha512) {
    throw new Error('Refusing to refresh DMG metadata while ZIP metadata is stale.')
  }

  const suffix = `.refresh-${process.pid}-${randomUUID()}.tmp`
  const temporaryBlockmap = `${paths.dmgBlockmap}${suffix}`
  const temporaryUpdateInfo = `${paths.updateInfo}${suffix}`

  try {
    const refreshedDmg = await buildBlockMap(paths.dmg, 'gzip', temporaryBlockmap)
    if (
      !Number.isSafeInteger(refreshedDmg?.size) ||
      refreshedDmg.size < 0 ||
      typeof refreshedDmg.sha512 !== 'string' ||
      !refreshedDmg.sha512
    ) {
      throw new Error('Electron Builder returned incomplete DMG blockmap metadata.')
    }

    const refreshedSource = replaceManagedUpdateFileRecord(originalSource, {
      name: names.dmg,
      sha512: refreshedDmg.sha512,
      size: refreshedDmg.size
    })
    const refreshedInfo = parseManagedUpdateInfo(refreshedSource)
    const refreshedZip = oneFileRecord(refreshedInfo, names.zip)
    const refreshedDmgRecord = oneFileRecord(refreshedInfo, names.dmg)

    if (
      refreshedInfo.path !== originalInfo.path ||
      refreshedInfo.sha512 !== originalInfo.sha512 ||
      refreshedZip.sha512 !== originalZip.sha512 ||
      refreshedZip.size !== originalZip.size
    ) {
      throw new Error('DMG metadata refresh changed the ZIP primary path or ZIP metadata.')
    }
    if (refreshedDmgRecord.sha512 !== refreshedDmg.sha512 || refreshedDmgRecord.size !== refreshedDmg.size) {
      throw new Error('DMG metadata refresh did not produce the expected appcast record.')
    }

    fs.writeFileSync(temporaryUpdateInfo, refreshedSource, 'utf8')
    fs.renameSync(temporaryBlockmap, paths.dmgBlockmap)
    fs.renameSync(temporaryUpdateInfo, paths.updateInfo)

    return {
      blockmap: names.dmgBlockmap,
      dmg: names.dmg,
      sha512: refreshedDmg.sha512,
      size: refreshedDmg.size,
      update_info: names.updateInfo
    }
  } finally {
    fs.rmSync(temporaryBlockmap, { force: true })
    fs.rmSync(temporaryUpdateInfo, { force: true })
  }
}

export async function refreshAndVerifyManagedUpdateRelease(options = {}) {
  const refreshed = await refreshManagedUpdateMetadata(options)
  const verified = await verifyManagedUpdateRelease(options)

  return { refreshed, verified }
}

if (isMain(import.meta.url)) {
  const [releaseDir = 'release', version, arch = DEFAULT_ARCH] = process.argv.slice(2)
  const result = await refreshAndVerifyManagedUpdateRelease({ arch, releaseDir, version })
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
}
