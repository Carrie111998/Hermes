#!/usr/bin/env node

import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

const [releaseDirArg = 'release', versionArg] = process.argv.slice(2)
const releaseDir = path.resolve(releaseDirArg)
const packageJson = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
const version = String(versionArg || packageJson.version || '').trim()

if (!version) {
  throw new Error('Expected an evaOS Agent version.')
}

const base = `evaOS-Agent-${version}-arm64`
const required = [
  `${base}.dmg`,
  `${base}.dmg.blockmap`,
  `${base}.zip`,
  `${base}.zip.blockmap`,
  'latest-mac.yml'
]

for (const name of required) {
  const filePath = path.join(releaseDir, name)
  if (!fs.statSync(filePath, { throwIfNoEntry: false })?.isFile()) {
    throw new Error(`Missing required signed-update asset: ${filePath}`)
  }
}

const updateInfoPath = path.join(releaseDir, 'latest-mac.yml')
const updateInfo = fs.readFileSync(updateInfoPath, 'utf8')
if (!new RegExp(`^version:\\s*${version.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*$`, 'm').test(updateInfo)) {
  throw new Error(`latest-mac.yml does not identify exact version ${version}.`)
}
if (!updateInfo.includes(`${base}.zip`) || !updateInfo.includes(`${base}.dmg`)) {
  throw new Error('latest-mac.yml must reference both the ZIP updater payload and DMG installer.')
}
if (/nousresearch|hermes-agent\/releases/i.test(updateInfo)) {
  throw new Error('latest-mac.yml contains an upstream Nous release reference.')
}

const assets = required.map(name => {
  const filePath = path.join(releaseDir, name)
  const bytes = fs.readFileSync(filePath)
  return {
    name,
    bytes: bytes.length,
    sha256: crypto.createHash('sha256').update(bytes).digest('hex')
  }
})

process.stdout.write(
  `${JSON.stringify(
    {
      schema_version: 'evaos.agent_update_release.v1',
      product: 'evaOS Agent',
      version,
      release_dir: releaseDir,
      assets
    },
    null,
    2
  )}\n`
)
