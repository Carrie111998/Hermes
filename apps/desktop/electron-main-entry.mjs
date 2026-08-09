// The production main bundle lives in dist/, which is intentionally unpacked
// so the embedded web server can serve static files from a real directory.
// Electron's ESM loader resolves package.json#main inside app.asar literally,
// though, so an unpacked dist/electron-main.mjs cannot be the package entry.
// Keep this tiny bridge inside app.asar and load the real bundle by filesystem
// path from app.asar.unpacked.

import { app } from 'electron'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const desktopRoot = fileURLToPath(new URL('.', import.meta.url))
const mainPath = app.isPackaged
  ? join(process.resourcesPath, 'app.asar.unpacked', 'dist', 'electron-main.mjs')
  : join(desktopRoot, 'dist', 'electron-main.mjs')

await import(pathToFileURL(mainPath).href)
