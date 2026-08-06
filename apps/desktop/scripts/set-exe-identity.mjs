#!/usr/bin/env node
// set-exe-identity.mjs — stamp the Hermes icon + version metadata onto the
// built Hermes.exe using resedit (pure-JS PE resource editor), completely
// decoupled from electron-builder's signing path.
//
// WHY THIS EXISTS
// ---------------
// apps/desktop/package.json sets build.win.signAndEditExecutable=false. That
// flag is load-bearing: turning electron-builder's own exe-editing ON also
// re-enables its signtool step, which fetches winCodeSign-2.6.0.7z, whose
// macOS symlinks crash 7-Zip on non-admin Windows (no Developer Mode = no
// SeCreateSymbolicLinkPrivilege). That is an unfixable dead end — we do NOT
// try to extract winCodeSign.
//
// The cost of disabling signAndEditExecutable is that electron-builder also
// skips its own resource editing, so the unpacked Hermes.exe keeps the stock
// Electron icon and "Electron" taskbar name. This script restores the icon +
// identity by editing PE resources DIRECTLY. resedit is a pure-JS PE resource
// editor: no signing, no certs, no winCodeSign, no symlinks, no native binary.
//
// HOW IT RUNS
// -----------
// Primarily as an electron-builder `afterPack` hook (scripts/after-pack.mjs),
// so EVERY packed build — first install, `hermes desktop`, the installer's
// --update rebuild, or a dev's manual `npm run pack` — gets a branded exe from
// one place. Previously this stamp lived only in install.ps1, so the update
// path (which rebuilds via `hermes desktop --build-only`, never install.ps1)
// shipped a stock "Electron" exe. Keeping it in afterPack closes that gap.
//
// Also runnable standalone for ad-hoc re-stamping:
//   node scripts/set-exe-identity.mjs <path-to-Hermes.exe>
//
// Exits 0 on success, non-zero on failure when run as a CLI. As a hook,
// stampExeIdentity() resolves on success and rejects on failure; the caller
// (after-pack.mjs) swallows the rejection so a stamp failure never fails an
// otherwise-good build (worst case: stock icon, not a broken app).

import { resolve, join } from 'node:path'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'

import { NtExecutable, NtExecutableResource, Data, Resource } from 'resedit'

import { isMain } from './utils.mjs'

const LANG_EN_US = { lang: 1033, codepage: 1200 }
const ICON_GROUP_ID = 1

// Stamp the Hermes icon + identity onto `exe`. Resolves on success, throws on
// failure. `desktopRoot` defaults to this script's package root so the icon and
// dependencies resolve regardless of cwd.
async function stampExeIdentity(exe, desktopRoot = resolve(import.meta.dirname, '..')) {
  if (!exe || !existsSync(exe)) {
    throw new Error(`target exe not found: ${exe}`)
  }

  const iconPath = join(desktopRoot, 'assets', 'icon.ico')
  if (!existsSync(iconPath)) {
    throw new Error(`icon not found: ${iconPath}`)
  }

  console.log(`[set-exe-identity] stamping ${exe}`)
  console.log(`[set-exe-identity] icon: ${iconPath}`)

  const peExe = NtExecutable.from(readFileSync(exe))
  const res = NtExecutableResource.from(peExe)

  const iconFile = Data.IconFile.from(readFileSync(iconPath))
  const icons = iconFile.icons.map(i => i.data)
  const iconGroups = Resource.IconGroupEntry.fromEntries(res.entries)
  const destinations = iconGroups.length > 0 ? iconGroups : [{ id: ICON_GROUP_ID, lang: 0 }]
  for (const { id, lang } of destinations) {
    Resource.IconGroupEntry.replaceIconsForResource(res.entries, id, lang, icons)
  }

  // Preserve Electron's fixed file/product versions and any existing strings;
  // only replace the branding fields that rcedit previously changed.
  const existing = Resource.VersionInfo.fromEntries(res.entries)
  const vi = existing[0] ?? Resource.VersionInfo.createEmpty()
  const languages = vi.getAllLanguagesForStringValues()
  for (const language of languages.length > 0 ? languages : [LANG_EN_US]) {
    vi.setStringValues(language, {
      ProductName: 'Hermes',
      FileDescription: 'Hermes',
      CompanyName: 'Nous Research',
      LegalCopyright: 'Copyright (c) 2026 Nous Research'
    })
  }
  vi.outputToResourceEntries(res.entries)

  res.outputResource(peExe)
  const output = peExe.generate()
  writeFileSync(exe, Buffer.from(output))

  console.log('[set-exe-identity] done — Hermes icon + identity stamped')
}

export { stampExeIdentity }

// CLI entry point: `node scripts/set-exe-identity.mjs <exe>`.
if (isMain(import.meta.url)) {
  const exe = process.argv[2]
  if (!exe) {
    console.error('[set-exe-identity] usage: set-exe-identity.mjs <path-to-exe>')
    process.exit(2)
  }
  stampExeIdentity(exe).catch(err => {
    console.error(`[set-exe-identity] ${err.message}`)
    process.exit(1)
  })
}
