import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'vitest'

const here = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(here, '../../..')
const updater = path.join(repoRoot, 'scripts', 'desktop-update', 'posix.sh')

function backupOutputDir(outputDir) {
  return path.join(path.dirname(outputDir), '.hermes-update-backup', path.basename(outputDir))
}

function writeBundle(bundle, executableText, { includeLazyChunk = true } = {}) {
  const resources = path.join(bundle, 'Contents', 'Resources')
  const dist = path.join(resources, 'app.asar.unpacked', 'dist')
  const assets = path.join(dist, 'assets')

  fs.mkdirSync(path.join(bundle, 'Contents', 'MacOS'), { recursive: true })
  fs.mkdirSync(assets, { recursive: true })
  fs.writeFileSync(path.join(bundle, 'Contents', 'MacOS', 'Hermes'), executableText)
  fs.writeFileSync(path.join(resources, 'app.asar'), 'asar')
  fs.writeFileSync(
    path.join(dist, 'index.html'),
    '<script type="module" src="./assets/index-test.js"></script>',
    'utf8'
  )
  fs.writeFileSync(
    path.join(assets, 'index-test.js'),
    'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/lazy-test.js"])))=>i.map(i=>d[i]);',
    'utf8'
  )

  if (includeLazyChunk) {
    fs.writeFileSync(path.join(assets, 'lazy-test.js'), 'export default {}', 'utf8')
  }
}

test.skipIf(process.platform !== 'darwin')(
  'posix updater restores the last complete mac bundle after a failed in-place build',
  () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-posix-mac-update-'))
    try {
      const installRoot = path.join(home, 'hermes-agent')
      const outputDir = path.join(installRoot, 'apps', 'desktop', 'release', 'mac-arm64')
      const bundle = path.join(outputDir, 'Hermes.app')
      const backupDir = backupOutputDir(outputDir)
      const backupBundle = path.join(backupDir, 'Hermes.app')
      const verifierDir = path.join(installRoot, 'apps', 'desktop', 'scripts')

      writeBundle(bundle, 'partial-new', { includeLazyChunk: false })
      writeBundle(backupBundle, 'previous-working')
      fs.mkdirSync(verifierDir, { recursive: true })
      fs.copyFileSync(
        path.join(repoRoot, 'apps', 'desktop', 'scripts', 'verify-mac-bundle.mjs'),
        path.join(verifierDir, 'verify-mac-bundle.mjs')
      )

      const run = spawnSync(
        '/bin/bash',
        [
          updater,
          '--self-test-mac-finalize',
          '--self-test-final-code',
          '6',
          '--install-root',
          installRoot,
          '--relaunch-target',
          bundle,
          '--no-ui'
        ],
        { encoding: 'utf8' }
      )

      assert.equal(run.status, 0, run.stderr || run.stdout)
      assert.equal(
        fs.readFileSync(path.join(bundle, 'Contents', 'MacOS', 'Hermes'), 'utf8'),
        'previous-working',
        run.stderr || run.stdout
      )
      assert.equal(fs.existsSync(backupDir), false)
    } finally {
      fs.rmSync(home, { recursive: true, force: true })
    }
  }
)

test.skipIf(process.platform !== 'darwin')(
  'posix updater restores the backup when the failed build produced no new app bundle',
  () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-posix-mac-update-'))
    try {
      const installRoot = path.join(home, 'hermes-agent')
      const outputDir = path.join(installRoot, 'apps', 'desktop', 'release', 'mac-arm64')
      const bundle = path.join(outputDir, 'Hermes.app')
      const backupBundle = path.join(backupOutputDir(outputDir), 'Hermes.app')
      const verifierDir = path.join(installRoot, 'apps', 'desktop', 'scripts')

      writeBundle(backupBundle, 'previous-working')
      fs.mkdirSync(verifierDir, { recursive: true })
      fs.copyFileSync(
        path.join(repoRoot, 'apps', 'desktop', 'scripts', 'verify-mac-bundle.mjs'),
        path.join(verifierDir, 'verify-mac-bundle.mjs')
      )

      const run = spawnSync(
        '/bin/bash',
        [
          updater,
          '--self-test-mac-finalize',
          '--self-test-final-code',
          '6',
          '--install-root',
          installRoot,
          '--relaunch-target',
          bundle,
          '--no-ui'
        ],
        { encoding: 'utf8' }
      )

      assert.equal(run.status, 0, run.stderr || run.stdout)
      assert.equal(
        fs.readFileSync(path.join(bundle, 'Contents', 'MacOS', 'Hermes'), 'utf8'),
        'previous-working',
        run.stderr || run.stdout
      )
    } finally {
      fs.rmSync(home, { recursive: true, force: true })
    }
  }
)
