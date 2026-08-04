import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFile } from 'node:child_process'

function run(command, args) {
  return new Promise((resolve, reject) => {
    execFile(command, args, (error, stdout, stderr) => {
      if (error) {
        reject(
          new Error(
            `${command} ${args.join(' ')} failed: ${stderr?.trim() || stdout?.trim() || error.message}`
          )
        )
        return
      }
      resolve({ stdout, stderr })
    })
  })
}

function inlineKeyLooksValid(value) {
  return value.includes('BEGIN PRIVATE KEY') && value.includes('END PRIVATE KEY')
}

function resolveApiKeyPath(rawValue) {
  const value = String(rawValue || '').trim()
  if (!value) return { keyPath: '', cleanup: () => {} }

  if (fs.existsSync(value)) {
    return { keyPath: value, cleanup: () => {} }
  }

  if (!inlineKeyLooksValid(value)) {
    throw new Error('APPLE_API_KEY must be a file path or inline .p8 key content')
  }

  const tempPath = path.join(os.tmpdir(), `hermes-notary-${Date.now()}-${process.pid}.p8`)
  fs.writeFileSync(tempPath, value, 'utf8')
  return {
    keyPath: tempPath,
    cleanup: () => {
      try {
        fs.rmSync(tempPath, { force: true })
      } catch {
        // Best-effort cleanup.
      }
    }
  }
}

export function resolveNotarizationConfig(env = process.env) {
  const profile = String(env.APPLE_NOTARY_PROFILE || '').trim()
  if (profile) {
    return { kind: 'profile', profile }
  }

  const keyId = String(env.APPLE_API_KEY_ID || '').trim()
  const issuer = String(env.APPLE_API_ISSUER || '').trim()
  const apiKey = String(env.APPLE_API_KEY || '').trim()
  if (apiKey && keyId && issuer) {
    return { apiKey, issuer, keyId, kind: 'api-key' }
  }

  if (env.EVAOS_MANAGED_RELEASE === '1') {
    throw new Error(
      'Managed release notarization requires APPLE_NOTARY_PROFILE or the complete APPLE_API_KEY, APPLE_API_KEY_ID, and APPLE_API_ISSUER set.'
    )
  }

  return null
}

export default async function notarize(context) {
  const { electronPlatformName, appOutDir, packager } = context
  if (electronPlatformName !== 'darwin') return

  const appName = packager.appInfo.productFilename
  const appPath = path.join(appOutDir, `${appName}.app`)
  if (!fs.existsSync(appPath)) {
    throw new Error(`Cannot notarize missing app bundle: ${appPath}`)
  }

  const notarization = resolveNotarizationConfig()
  if (!notarization) {
    console.log(
      'Skipping notarization for a non-release build: no complete Apple notarization credentials are configured.'
    )
    return
  }

  if (notarization.kind === 'profile') {
    const zipPath = path.join(appOutDir, `${appName}.zip`)
    await run('ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', appPath, zipPath])
    await run('xcrun', ['notarytool', 'submit', zipPath, '--keychain-profile', notarization.profile, '--wait'])
    await run('xcrun', ['stapler', 'staple', '-v', appPath])
    try {
      fs.rmSync(zipPath, { force: true })
    } catch {
      // Best-effort cleanup.
    }
    return
  }

  const { keyPath, cleanup } = resolveApiKeyPath(notarization.apiKey)
  const zipPath = path.join(appOutDir, `${appName}.zip`)
  try {
    await run('ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', appPath, zipPath])
    await run('xcrun', [
      'notarytool',
      'submit',
      zipPath,
      '--key',
      keyPath,
      '--key-id',
      notarization.keyId,
      '--issuer',
      notarization.issuer,
      '--wait'
    ])
    await run('xcrun', ['stapler', 'staple', '-v', appPath])
  } finally {
    try {
      fs.rmSync(zipPath, { force: true })
    } catch {
      // Best-effort cleanup.
    }
    cleanup()
  }
}
