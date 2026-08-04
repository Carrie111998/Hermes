#!/usr/bin/env node

import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFile } from 'node:child_process'

import { isMain } from './utils.mjs'

const DEFAULT_ARCH = 'arm64'
const packageJson = JSON.parse(fs.readFileSync(new URL('../package.json', import.meta.url), 'utf8'))

function runCommand(command, args) {
  return new Promise((resolve, reject) => {
    execFile(command, args, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(`${command} ${args.join(' ')} failed: ${stderr?.trim() || stdout?.trim() || error.message}`))
        return
      }

      resolve({ stderr: String(stderr || ''), stdout: String(stdout || '') })
    })
  })
}

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

export function parseManagedUpdateInfo(source) {
  const result = { files: [], path: '', sha512: '', version: '' }
  let currentFile = null
  let inFiles = false

  const finishCurrentFile = () => {
    if (currentFile) {
      result.files.push(currentFile)
      currentFile = null
    }
  }

  for (const line of String(source || '').split(/\r?\n/)) {
    const topLevel = line.match(/^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$/)
    if (topLevel) {
      finishCurrentFile()
      inFiles = topLevel[1] === 'files'

      if (topLevel[1] === 'version') result.version = unquoteYamlScalar(topLevel[2])
      if (topLevel[1] === 'path') result.path = unquoteYamlScalar(topLevel[2])
      if (topLevel[1] === 'sha512') result.sha512 = unquoteYamlScalar(topLevel[2])
      continue
    }

    if (!inFiles) {
      continue
    }

    const url = line.match(/^\s*-\s+url:\s*(.+)$/)
    if (url) {
      finishCurrentFile()
      currentFile = { sha512: '', size: Number.NaN, url: unquoteYamlScalar(url[1]) }
      continue
    }

    const field = line.match(/^\s+(sha512|size):\s*(.+)$/)
    if (!field || !currentFile) {
      continue
    }

    if (field[1] === 'sha512') currentFile.sha512 = unquoteYamlScalar(field[2])
    if (field[1] === 'size') currentFile.size = Number.parseInt(unquoteYamlScalar(field[2]), 10)
  }

  finishCurrentFile()

  return result
}

export function renderArtifactName(manifest, { arch, ext, version }) {
  const template = manifest?.build?.artifactName
  if (typeof template !== 'string' || !template.trim()) {
    throw new Error('package.json build.artifactName is required for release verification.')
  }

  const rendered = template.replaceAll('${version}', version).replaceAll('${arch}', arch).replaceAll('${ext}', ext)

  if (rendered.includes('${')) {
    throw new Error(`Unsupported electron-builder artifact template: ${template}`)
  }

  return rendered
}

async function hashFile(filePath) {
  const sha256 = crypto.createHash('sha256')
  const sha512 = crypto.createHash('sha512')
  let bytes = 0

  await new Promise((resolve, reject) => {
    const stream = fs.createReadStream(filePath)
    stream.on('data', chunk => {
      bytes += chunk.length
      sha256.update(chunk)
      sha512.update(chunk)
    })
    stream.once('error', reject)
    stream.once('end', resolve)
  })

  return {
    bytes,
    sha256: sha256.digest('hex'),
    sha512: sha512.digest('base64')
  }
}

async function plistValue(run, plistPath, key) {
  const result = await run('plutil', ['-extract', key, 'raw', '-o', '-', plistPath])

  return result.stdout.trim()
}

async function verifyPackagedApp({ appPath, arch, manifest, run, version }) {
  const productName = manifest.build?.productName || manifest.productName
  const executableName = manifest.build?.executableName || productName
  const appId = manifest.build?.appId
  const plistPath = path.join(appPath, 'Contents', 'Info.plist')
  const executablePath = path.join(appPath, 'Contents', 'MacOS', executableName)

  if (!fs.statSync(plistPath, { throwIfNoEntry: false })?.isFile()) {
    throw new Error(`Missing packaged Info.plist: ${plistPath}`)
  }
  if (!fs.statSync(executablePath, { throwIfNoEntry: false })?.isFile()) {
    throw new Error(`Missing packaged executable: ${executablePath}`)
  }

  const identity = {
    bundleId: await plistValue(run, plistPath, 'CFBundleIdentifier'),
    displayName: await plistValue(run, plistPath, 'CFBundleDisplayName'),
    executableName: await plistValue(run, plistPath, 'CFBundleExecutable'),
    shortVersion: await plistValue(run, plistPath, 'CFBundleShortVersionString')
  }

  if (identity.bundleId !== appId) {
    throw new Error(`Unexpected bundle identifier in ${appPath}: ${identity.bundleId}`)
  }
  if (identity.displayName !== productName) {
    throw new Error(`Unexpected display name in ${appPath}: ${identity.displayName}`)
  }
  if (identity.executableName !== executableName) {
    throw new Error(`Unexpected executable name in ${appPath}: ${identity.executableName}`)
  }
  if (identity.shortVersion !== version) {
    throw new Error(`Unexpected bundle version in ${appPath}: ${identity.shortVersion}`)
  }

  const architectures = (await run('lipo', ['-archs', executablePath])).stdout.trim().split(/\s+/).filter(Boolean)
  if (architectures.length !== 1 || architectures[0] !== arch) {
    throw new Error(`Expected an ${arch}-only executable in ${appPath}; found ${architectures.join(', ')}`)
  }

  await run('codesign', ['--verify', '--deep', '--strict', '--verbose=4', appPath])
  const signature = await run('codesign', ['--display', '--verbose=4', appPath])
  const signatureText = `${signature.stdout}\n${signature.stderr}`
  if (!/Authority=Developer ID Application:/i.test(signatureText)) {
    throw new Error(`Packaged app is not signed by a Developer ID Application identity: ${appPath}`)
  }
  if (!/TeamIdentifier=(?!not set)\S+/i.test(signatureText)) {
    throw new Error(`Packaged app has no signing team identifier: ${appPath}`)
  }
  if (!/flags=.*\bruntime\b/i.test(signatureText)) {
    throw new Error(`Packaged app is missing the hardened runtime flag: ${appPath}`)
  }

  const gatekeeper = await run('spctl', ['--assess', '--type', 'execute', '--verbose=4', appPath])
  const gatekeeperText = `${gatekeeper.stdout}\n${gatekeeper.stderr}`
  if (!/\baccepted\b/i.test(gatekeeperText) || !/source=Notarized Developer ID/i.test(gatekeeperText)) {
    throw new Error(`Gatekeeper did not accept ${appPath} as Notarized Developer ID.`)
  }

  await run('xcrun', ['stapler', 'validate', appPath])

  return { architectures, identity }
}

async function verifyAppsFromArtifacts({ arch, dmgPath, manifest, run, version, zipPath }) {
  const productName = manifest.build?.productName || manifest.productName
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'evaos-agent-release-verify-'))
  const zipRoot = path.join(scratch, 'zip')
  const dmgRoot = path.join(scratch, 'dmg')
  fs.mkdirSync(zipRoot)
  fs.mkdirSync(dmgRoot)
  let dmgAttached = false

  try {
    await run('ditto', ['-x', '-k', zipPath, zipRoot])
    const zipApp = await verifyPackagedApp({
      appPath: path.join(zipRoot, `${productName}.app`),
      arch,
      manifest,
      run,
      version
    })

    await run('hdiutil', ['attach', '-readonly', '-nobrowse', '-mountpoint', dmgRoot, dmgPath])
    dmgAttached = true
    const dmgApp = await verifyPackagedApp({
      appPath: path.join(dmgRoot, `${productName}.app`),
      arch,
      manifest,
      run,
      version
    })

    return { dmg: dmgApp, zip: zipApp }
  } finally {
    if (dmgAttached) {
      try {
        await run('hdiutil', ['detach', dmgRoot])
      } catch {
        // Preserve the original verification failure; cleanup is best effort.
      }
    }
    fs.rmSync(scratch, { force: true, recursive: true })
  }
}

export async function verifyManagedUpdateRelease(options = {}) {
  const manifest = options.manifest ?? packageJson
  const releaseDir = path.resolve(options.releaseDir ?? 'release')
  const version = String(options.version || manifest.version || '').trim()
  const arch = String(options.arch || DEFAULT_ARCH).trim()
  const platform = options.platform ?? process.platform
  const run = options.runCommand ?? runCommand

  if (!version) {
    throw new Error('Expected an evaOS Agent version.')
  }
  if (arch !== DEFAULT_ARCH) {
    throw new Error(`Managed test release verification is arm64-only; received ${arch}.`)
  }
  if (platform !== 'darwin') {
    throw new Error('Managed macOS release verification must run on macOS.')
  }

  const names = {
    dmg: renderArtifactName(manifest, { arch, ext: 'dmg', version }),
    dmgBlockmap: renderArtifactName(manifest, { arch, ext: 'dmg.blockmap', version }),
    updateInfo: 'latest-mac.yml',
    zip: renderArtifactName(manifest, { arch, ext: 'zip', version }),
    zipBlockmap: renderArtifactName(manifest, { arch, ext: 'zip.blockmap', version })
  }
  const required = [names.dmg, names.dmgBlockmap, names.zip, names.zipBlockmap, names.updateInfo]

  for (const name of required) {
    const filePath = path.join(releaseDir, name)
    if (!fs.statSync(filePath, { throwIfNoEntry: false })?.isFile()) {
      throw new Error(`Missing required signed-update asset: ${filePath}`)
    }
  }

  const updateInfoPath = path.join(releaseDir, names.updateInfo)
  const updateInfoSource = fs.readFileSync(updateInfoPath, 'utf8')
  const updateInfo = parseManagedUpdateInfo(updateInfoSource)
  if (updateInfo.version !== version) {
    throw new Error(`latest-mac.yml does not identify exact version ${version}.`)
  }
  if (/nousresearch|hermes-agent\/releases/i.test(updateInfoSource)) {
    throw new Error('latest-mac.yml contains an upstream Nous release reference.')
  }

  const assets = []
  for (const name of required) {
    assets.push({ name, ...(await hashFile(path.join(releaseDir, name))) })
  }
  const byName = new Map(assets.map(asset => [asset.name, asset]))

  for (const name of [names.zip, names.dmg]) {
    const records = updateInfo.files.filter(record => record.url === name)
    if (records.length !== 1) {
      throw new Error(`latest-mac.yml must contain exactly one file record for ${name}.`)
    }
    const asset = byName.get(name)
    const record = records[0]
    if (record.size !== asset.bytes) {
      throw new Error(`latest-mac.yml size does not match ${name}.`)
    }
    if (record.sha512 !== asset.sha512) {
      throw new Error(`latest-mac.yml SHA-512 does not match ${name}.`)
    }
  }

  const zipAsset = byName.get(names.zip)
  if (updateInfo.path !== names.zip || updateInfo.sha512 !== zipAsset.sha512) {
    throw new Error('latest-mac.yml primary updater path and SHA-512 must identify the ZIP payload.')
  }

  const packagedApps = await verifyAppsFromArtifacts({
    arch,
    dmgPath: path.join(releaseDir, names.dmg),
    manifest,
    run,
    version,
    zipPath: path.join(releaseDir, names.zip)
  })

  return {
    arch,
    assets,
    packaged_apps: packagedApps,
    product: manifest.build?.productName || manifest.productName,
    release_dir: releaseDir,
    schema_version: 'evaos.agent_update_release.v2',
    version
  }
}

if (isMain(import.meta.url)) {
  const [releaseDir = 'release', version, arch = DEFAULT_ARCH] = process.argv.slice(2)
  const result = await verifyManagedUpdateRelease({ arch, releaseDir, version })
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
}
