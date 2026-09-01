import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const TAG_WITH_URL = /<(?:script|link)\b[^>]*\b(?:src|href)=["']([^"']+)["'][^>]*>/gi
const MODULE_TAG = /\btype=["']module["']|\brel=["']modulepreload["']/i
const MAP_DEPS_ARRAY = /__vite__mapDeps[^[\]]{0,300}\[([^\]]*)\]/g
const QUOTED_REF = /["']([^"']+)["']/g

function localRef(value) {
  if (!value || /^[a-z]+:|^\/\//i.test(value)) {
    return null
  }

  return value.replace(/^\.\//, '').split(/[?#]/)[0]
}

function moduleRefs(html) {
  const refs = []

  for (const [tag, href] of String(html ?? '').matchAll(TAG_WITH_URL)) {
    const ref = MODULE_TAG.test(tag) ? localRef(href) : null

    if (ref) {
      refs.push(ref)
    }
  }

  return refs
}

function lazyRefs(js) {
  const refs = new Set()

  for (const [, body] of String(js ?? '').matchAll(MAP_DEPS_ARRAY)) {
    for (const [, value] of body.matchAll(QUOTED_REF)) {
      const ref = localRef(value)

      if (ref) {
        refs.add(ref)
      }
    }
  }

  return [...refs]
}

function readUtf8(file) {
  try {
    return fs.readFileSync(file, 'utf8')
  } catch {
    return null
  }
}

export function verifyMacBundle(bundlePath, productFilename = 'Hermes') {
  const bundle = path.resolve(String(bundlePath || ''))
  const resources = path.join(bundle, 'Contents', 'Resources')
  const dist = path.join(resources, 'app.asar.unpacked', 'dist')
  const indexPath = path.join(dist, 'index.html')
  const required = [
    ['Contents/MacOS/' + productFilename, path.join(bundle, 'Contents', 'MacOS', productFilename)],
    ['Contents/Resources/app.asar', path.join(resources, 'app.asar')],
    ['Contents/Resources/app.asar.unpacked/dist/index.html', indexPath]
  ]
  const missing = required.filter(([, file]) => !fs.existsSync(file)).map(([label]) => label)

  if (missing.length > 0) {
    return { ok: false, missing }
  }

  const html = readUtf8(indexPath)
  const bootRefs = moduleRefs(html)

  if (html === null || bootRefs.length === 0) {
    return { ok: false, missing: ['renderer module graph'] }
  }

  const queue = [...bootRefs]
  const seen = new Set()

  while (queue.length > 0) {
    const ref = queue.shift()

    if (seen.has(ref)) {
      continue
    }

    seen.add(ref)
    const file = path.join(dist, ref)

    if (!fs.existsSync(file)) {
      missing.push(ref)
      continue
    }

    if (!/\.m?js$/i.test(ref)) {
      continue
    }

    const js = readUtf8(file)

    if (js === null) {
      missing.push(ref)
      continue
    }

    for (const lazyRef of lazyRefs(js)) {
      const chunkRelative = path.join(path.dirname(ref), lazyRef).split(path.sep).join('/')

      if (fs.existsSync(path.join(dist, lazyRef))) {
        queue.push(lazyRef)
      } else if (fs.existsSync(path.join(dist, chunkRelative))) {
        queue.push(chunkRelative)
      } else {
        queue.push(lazyRef)
      }
    }
  }

  return { ok: missing.length === 0, missing: [...new Set(missing)] }
}

export function macRollbackOutputDir(outputDir) {
  return path.join(path.dirname(outputDir), '.hermes-update-backup', path.basename(outputDir))
}

export function finalizeMacBundleUpdate(bundlePath, { productFilename = 'Hermes', updateSucceeded = false } = {}) {
  const bundle = path.resolve(String(bundlePath || ''))
  const outputDir = path.dirname(bundle)
  const backupOutputDir = macRollbackOutputDir(outputDir)
  const backupBundle = path.join(backupOutputDir, path.basename(bundle))
  const newBundle = verifyMacBundle(bundle, productFilename)

  if (updateSucceeded && newBundle.ok) {
    return {
      action: 'kept-new',
      backupBundle,
      newBundleMissing: [],
      usable: true
    }
  }

  const backup = verifyMacBundle(backupBundle, productFilename)

  if (!backup.ok) {
    return {
      action: 'rollback-unavailable',
      backupBundle,
      backupMissing: backup.missing,
      newBundleMissing: newBundle.missing,
      usable: newBundle.ok
    }
  }

  const failedOutputDir = `${backupOutputDir}.failed`

  try {
    fs.rmSync(failedOutputDir, { recursive: true, force: true })

    if (fs.existsSync(outputDir)) {
      fs.renameSync(outputDir, failedOutputDir)
    }

    try {
      fs.renameSync(backupOutputDir, outputDir)
    } catch (error) {
      if (fs.existsSync(failedOutputDir) && !fs.existsSync(outputDir)) {
        fs.renameSync(failedOutputDir, outputDir)
      }

      throw error
    }

    fs.rmSync(failedOutputDir, { recursive: true, force: true })

    return {
      action: 'restored-backup',
      backupBundle,
      newBundleMissing: newBundle.missing,
      usable: true
    }
  } catch (error) {
    return {
      action: 'rollback-failed',
      backupBundle,
      error: error instanceof Error ? error.message : String(error),
      newBundleMissing: newBundle.missing,
      usable: verifyMacBundle(bundle, productFilename).ok
    }
  }
}

function canonicalPath(file) {
  try {
    return fs.realpathSync(file)
  } catch {
    return path.resolve(file)
  }
}

if (process.argv[1] && canonicalPath(process.argv[1]) === canonicalPath(fileURLToPath(import.meta.url))) {
  const finalize = process.argv[2] === '--finalize'
  const result = finalize
    ? finalizeMacBundleUpdate(process.argv[3], {
        productFilename: process.argv[4] || 'Hermes',
        updateSucceeded: process.argv[5] === 'success'
      })
    : verifyMacBundle(process.argv[2], process.argv[3] || 'Hermes')
  process.stdout.write(`${JSON.stringify(result)}\n`)
  process.exitCode = finalize ? (result.usable ? 0 : 1) : result.ok ? 0 : 1
}
