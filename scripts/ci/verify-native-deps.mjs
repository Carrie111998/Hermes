import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'

const require = createRequire(import.meta.url)

function packageRoot(packageName) {
  let directory = path.dirname(require.resolve(packageName))
  while (true) {
    const manifest = path.join(directory, 'package.json')
    if (fs.existsSync(manifest)) {
      const parsed = JSON.parse(fs.readFileSync(manifest, 'utf-8'))
      if (parsed.name === packageName) return directory
    }
    const parent = path.dirname(directory)
    if (parent === directory) throw new Error(`Unable to find package root for ${packageName}`)
    directory = parent
  }
}

function filesBelow(directory) {
  return fs.readdirSync(directory, { recursive: true }).map((entry) => path.join(directory, entry))
}

const electronRoot = packageRoot('electron')
const electronExecutable = {
  darwin: path.join(electronRoot, 'dist', 'Electron.app', 'Contents', 'MacOS', 'Electron'),
  linux: path.join(electronRoot, 'dist', 'electron'),
  win32: path.join(electronRoot, 'dist', 'electron.exe')
}[process.platform]
assert.ok(electronExecutable, `Unsupported verification platform: ${process.platform}`)
fs.accessSync(electronExecutable, fs.constants.X_OK)
console.log(`Electron executable: ${electronExecutable}`)

const extractorRoot = packageRoot('@electron-internal/extract-zip')
const nativeExtractorFiles = filesBelow(extractorRoot).filter((file) => file.endsWith('.node'))
assert.deepEqual(nativeExtractorFiles, [], 'Electron extractor unexpectedly contains native addons')
const extractor = require('@electron-internal/extract-zip')
assert.equal(typeof extractor.extract, 'function')
console.log(`Pure-JavaScript extractor: ${extractorRoot}`)

const getWindowsRoot = packageRoot('get-windows')
const getWindowsManifest = path.join(getWindowsRoot, 'package.json')
const getWindowsRequire = createRequire(getWindowsManifest)
const preGyp = getWindowsRequire('@mapbox/node-pre-gyp')
const binding = preGyp.find(getWindowsManifest)
fs.accessSync(binding, fs.constants.R_OK)
if (process.platform === 'win32') {
  getWindowsRequire(binding)
  console.log(`get-windows binding: ${binding}`)
} else {
  const getWindows = await import('get-windows')
  assert.equal(typeof getWindows.activeWindow, 'function')
  console.log(`get-windows runtime: ${getWindowsRoot}`)
}
