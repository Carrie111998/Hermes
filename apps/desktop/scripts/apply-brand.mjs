#!/usr/bin/env node
// apply-brand.mjs — resolve the build flavor (DESKTOP_BRAND) into concrete
// build inputs so one repo ships multiple branded desktop apps.
//
// The repo hosts strictly-separated desktop brands built from the same
// Hermes core: `ix-agency` (default), `quizverse`, and `foundrly`. A brand
// manifest in apps/desktop/brands/<id>.json owns everything identity-shaped:
// appId, productName, artifact naming, icons, update feed, S3 publish path,
// and which brand workspace ships in the renderer.
//
// Outputs (both under apps/desktop/build/, which is gitignored):
//   build/brand.json                   — the resolved manifest (consumed by
//                                        write-build-stamp / diagnostics; the
//                                        electron main + renderer bundles bake
//                                        the brand in at bundle time via
//                                        HERMES_DESKTOP_BRAND defines, not by
//                                        reading this file at runtime)
//   build/electron-builder-brand.json  — package.json `build` block with the
//                                        brand overlay applied; passed to
//                                        electron-builder via --config so the
//                                        packaged appId/productName/artifacts/
//                                        publish path all follow the brand
//
// Usage:
//   DESKTOP_BRAND=quizverse node scripts/apply-brand.mjs
//   (no env → ix-agency)

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const desktopRoot = path.resolve(here, '..')

export const KNOWN_BRANDS = ['ix-agency', 'quizverse', 'foundrly']
export const DEFAULT_BRAND = 'ix-agency'

export function resolveBrandId(raw = process.env.DESKTOP_BRAND) {
  const id = String(raw || '').trim() || DEFAULT_BRAND

  if (!KNOWN_BRANDS.includes(id)) {
    throw new Error(
      `Unknown DESKTOP_BRAND "${id}" — expected one of: ${KNOWN_BRANDS.join(', ')}`
    )
  }

  return id
}

export function loadBrand(raw = process.env.DESKTOP_BRAND) {
  const id = resolveBrandId(raw)
  const manifestPath = path.join(desktopRoot, 'brands', `${id}.json`)
  const brand = JSON.parse(fs.readFileSync(manifestPath, 'utf8'))

  if (brand.id !== id) {
    throw new Error(`Brand manifest ${manifestPath} declares id "${brand.id}", expected "${id}"`)
  }

  return brand
}

/** package.json `build` block with the brand identity overlaid. */
export function brandedBuilderConfig(brand, pkg) {
  const build = structuredClone(pkg.build)

  build.appId = brand.appId
  build.productName = brand.productName
  build.artifactName = `${brand.artifactPrefix}-\${version}-\${os}-\${arch}.\${ext}`
  build.icon = brand.icon
  build.protocols = [{ name: `${brand.productName} Protocol`, schemes: [brand.protocolScheme] }]
  // The packaged app's package.json drives app.getName() before main.ts runs.
  build.extraMetadata = { ...build.extraMetadata, description: brand.description, productName: brand.productName }

  build.mac = {
    ...build.mac,
    extendInfo: {
      ...build.mac?.extendInfo,
      CFBundleDisplayName: brand.productName,
      CFBundleExecutable: brand.productName,
      CFBundleName: brand.productName,
      NSAudioCaptureUsageDescription: `${brand.productName} uses audio capture for voice conversations.`,
      NSMicrophoneUsageDescription: `${brand.productName} uses the microphone for voice input and voice conversations.`
    }
  }
  build.dmg = { ...build.dmg, title: `Install ${brand.productName}` }
  build.win = { ...build.win, legalTrademarks: brand.productName }
  build.linux = {
    ...build.linux,
    executableName: brand.executableName,
    synopsis: brand.description
  }
  build.nsis = {
    ...build.nsis,
    shortcutName: brand.productName,
    uninstallDisplayName: brand.productName
  }
  build.publish = (build.publish || []).map(entry =>
    entry && entry.provider === 's3' ? { ...entry, path: brand.s3PublishPath } : entry
  )

  // Don't ship the other brands' mark assets: public/ (and its dist/ copy)
  // is packaged wholesale, so exclude every markSvg that isn't this brand's.
  const otherMarks = KNOWN_BRANDS.filter(id => id !== brand.id)
    .map(id => loadBrand(id).markSvg)
    .filter(mark => mark && mark !== brand.markSvg)

  if (otherMarks.length > 0) {
    build.files = [...(build.files || []), ...otherMarks.flatMap(mark => [`!dist/${mark}`, `!public/${mark}`])]
  }

  // Orphaned Hermes marketing art — not used by either branded desktop app.
  const hermesArtExcludes = [
    '!public/hermes.png',
    '!dist/hermes.png',
    '!public/hermes-sprite.png',
    '!dist/hermes-sprite.png',
    '!public/hermes-frames',
    '!public/hermes-frames/**',
    '!dist/hermes-frames',
    '!dist/hermes-frames/**',
    '!public/nous-girl.jpg',
    '!dist/nous-girl.jpg'
  ]

  build.files = [...(build.files || []), ...hermesArtExcludes]

  // Per-brand asset dirs: each brand's art must not ship in the others.
  if (brand.id === 'quizverse') {
    build.files = [
      ...(build.files || []),
      '!public/ix-agency-mark.svg',
      '!dist/ix-agency-mark.svg',
      '!public/apple-touch-icon.png',
      '!dist/apple-touch-icon.png',
      '!public/foundrly',
      '!public/foundrly/**',
      '!dist/foundrly',
      '!dist/foundrly/**'
    ]
  } else if (brand.id === 'foundrly') {
    build.files = [
      ...(build.files || []),
      '!public/ix-agency-mark.svg',
      '!dist/ix-agency-mark.svg',
      '!public/apple-touch-icon.png',
      '!dist/apple-touch-icon.png',
      '!public/quizverse',
      '!public/quizverse/**',
      '!dist/quizverse',
      '!dist/quizverse/**'
    ]
  } else {
    build.files = [
      ...(build.files || []),
      '!public/quizverse',
      '!public/quizverse/**',
      '!dist/quizverse',
      '!dist/quizverse/**',
      '!public/foundrly',
      '!public/foundrly/**',
      '!dist/foundrly',
      '!dist/foundrly/**'
    ]
  }

  // The exe-stamp icon rides along as an extraResource; point it at the
  // brand's .ico when one exists (falls back to the default brand's icon so
  // packaging never breaks on a brand that ships only a .png).
  const icoSource = fs.existsSync(path.join(desktopRoot, brand.iconIco)) ? brand.iconIco : 'assets/icon.ico'

  build.extraResources = (build.extraResources || []).map(entry =>
    entry && entry.to === 'icon.ico' ? { ...entry, from: icoSource } : entry
  )

  if (brand.id === 'quizverse') {
    build.extraResources = [
      ...(build.extraResources || []),
      {
        from: '../../packages/quizverse-mcp',
        to: 'quizverse-mcp',
        filter: [
          'server.mjs',
          'relay.mjs',
          'contracts.mjs',
          'contracts.d.mts',
          'response-contracts.mjs',
          'response-contracts.d.mts',
          'quiz-fetch-contracts.mjs',
          'quiz-fetch-contracts.d.mts',
          'package.json',
          'README.md'
        ]
      },
      {
        from: 'brands/quizverse-skills',
        to: 'quizverse-skills'
      }
    ]
  }

  if (brand.id === 'foundrly') {
    build.extraResources = [
      ...(build.extraResources || []),
      {
        from: 'brands/foundrly-skills',
        to: 'foundrly-skills'
      }
    ]
  }

  return build
}

export function applyBrand(raw = process.env.DESKTOP_BRAND) {
  const brand = loadBrand(raw)
  const pkg = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'package.json'), 'utf8'))
  const buildDir = path.join(desktopRoot, 'build')

  fs.mkdirSync(buildDir, { recursive: true })
  fs.writeFileSync(path.join(buildDir, 'brand.json'), `${JSON.stringify(brand, null, 2)}\n`, 'utf8')
  fs.writeFileSync(
    path.join(buildDir, 'electron-builder-brand.json'),
    `${JSON.stringify(brandedBuilderConfig(brand, pkg), null, 2)}\n`,
    'utf8'
  )

  return brand
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)

if (isMain) {
  const brand = applyBrand()
  console.log(`[apply-brand] brand: ${brand.id} (${brand.productName}) → build/brand.json + build/electron-builder-brand.json`)
}
