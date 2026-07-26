#!/usr/bin/env node
// check-brand-separation.mjs — assert a built desktop bundle contains ONLY
// the active brand's workspace.
//
// Strict brand separation is a release requirement: an IX Agency build must
// ship no QuizVerse/Foundrly rail item / route / workspace chunk, and vice
// versa. The renderer achieves that with compile-time brand constants
// (VITE_DESKTOP_BRAND define → dead-code-eliminated lazy imports); this script
// proves it on the actual build output.
//
// Run AFTER `npm run build` for the brand under test:
//   DESKTOP_BRAND=foundrly npm run build
//   DESKTOP_BRAND=foundrly node scripts/check-brand-separation.mjs

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { KNOWN_BRANDS, resolveBrandId } from './apply-brand.mjs'

const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const brandId = resolveBrandId()
const otherBrands = KNOWN_BRANDS.filter(id => id !== brandId)

const WORKSPACE_MARKERS = {
  'ix-agency': ['Search invoices', 'Org skills'],
  quizverse: ['persist:quizverse-tutor', 'persist:quizverse-web', 'TutorX'],
  foundrly: ['persist:foundrly-home', 'Overnight visibility', 'Foundrly home']
}

const MAIN_MARKERS = {
  'ix-agency': ['hermes:ix-agency:', 'persist:ix-agency-portal', 'wg-quick'],
  quizverse: ['hermes:quizverse:', 'tutor.intelli-verse-x.ai', 'DeepTutorSupervisor', 'persist:quizverse-tutor'],
  foundrly: ['hermes:foundrly:', 'persist:foundrly-home']
}

const IPC_PREFIX = {
  'ix-agency': 'hermes:ix-agency:',
  quizverse: 'hermes:quizverse:',
  foundrly: 'hermes:foundrly:'
}

const CHUNK_NAME = {
  'ix-agency': 'ix-agency',
  quizverse: 'quizverse',
  foundrly: 'foundrly'
}

const failures = []

function fail(message) {
  failures.push(message)
  console.error(`  ✗ ${message}`)
}

function ok(message) {
  console.log(`  ✓ ${message}`)
}

console.log(`[check-brand-separation] brand: ${brandId}`)

const brandJsonPath = path.join(desktopRoot, 'build', 'brand.json')
let brand = null

try {
  brand = JSON.parse(fs.readFileSync(brandJsonPath, 'utf8'))
} catch {
  fail(`build/brand.json missing or unreadable — run the build first (${brandJsonPath})`)
}

if (brand) {
  if (brand.id === brandId) {
    ok(`build/brand.json is ${brand.id} (${brand.productName})`)
  } else {
    fail(`build/brand.json is "${brand.id}" but DESKTOP_BRAND resolved to "${brandId}" — stale build/`)
  }
}

const builderConfigPath = path.join(desktopRoot, 'build', 'electron-builder-brand.json')

if (brand && fs.existsSync(builderConfigPath)) {
  const config = JSON.parse(fs.readFileSync(builderConfigPath, 'utf8'))
  const expectations = [
    ['appId', config.appId, brand.appId],
    ['productName', config.productName, brand.productName],
    ['artifactName', config.artifactName, `${brand.artifactPrefix}-\${version}-\${os}-\${arch}.\${ext}`],
    ['linux.executableName', config.linux?.executableName, brand.executableName],
    ['publish[s3].path', (config.publish || []).find(p => p?.provider === 's3')?.path, brand.s3PublishPath]
  ]

  for (const [label, actual, expected] of expectations) {
    if (actual === expected) {
      ok(`builder config ${label} = ${actual}`)
    } else {
      fail(`builder config ${label} is "${actual}", expected "${expected}"`)
    }
  }
} else if (brand) {
  fail(`build/electron-builder-brand.json missing (${builderConfigPath})`)
}

const assetsDir = path.join(desktopRoot, 'dist', 'assets')

if (!fs.existsSync(assetsDir)) {
  fail(`dist/assets missing — run \`DESKTOP_BRAND=${brandId} npm run build\` first`)
} else {
  const chunkFiles = fs.readdirSync(assetsDir).filter(name => name.endsWith('.js'))
  const chunkText = chunkFiles.map(name => fs.readFileSync(path.join(assetsDir, name), 'utf8')).join('\n')

  for (const otherBrand of otherBrands) {
    const foreignChunks = chunkFiles.filter(name => name.includes(CHUNK_NAME[otherBrand]))

    if (foreignChunks.length === 0) {
      ok(`no ${otherBrand} workspace chunk among ${chunkFiles.length} renderer chunks`)
    } else {
      fail(`${otherBrand} workspace chunk(s) present: ${foreignChunks.join(', ')}`)
    }

    for (const marker of WORKSPACE_MARKERS[otherBrand]) {
      if (chunkText.includes(marker)) {
        fail(`renderer bundle leaks ${otherBrand} marker "${marker}"`)
      } else {
        ok(`no ${otherBrand} marker "${marker}"`)
      }
    }
  }

  const ownPresent = WORKSPACE_MARKERS[brandId].filter(marker => chunkText.includes(marker))

  if (ownPresent.length > 0) {
    ok(`own workspace markers present (${ownPresent.length}/${WORKSPACE_MARKERS[brandId].length})`)
  } else {
    fail(`none of the ${brandId} workspace markers found — markers drifted or the workspace was dropped`)
  }

  for (const otherBrand of otherBrands) {
    const otherManifest = JSON.parse(fs.readFileSync(path.join(desktopRoot, 'brands', `${otherBrand}.json`), 'utf8'))
    const countOccurrences = (haystack, needle) => haystack.split(needle).length - 1

    // IX Agency admin skills legitimately name managed apps (QuizVerse, Foundrly, …).
    if (brandId === 'ix-agency') {
      ok(`skipped "${otherManifest.productName}" product-name scan — IX admin surfaces reference managed apps`)
      continue
    }

    const productNameHits = countOccurrences(chunkText, otherManifest.productName)

    if (productNameHits === 0) {
      ok(`no "${otherManifest.productName}" product-name occurrences in the renderer bundle`)
    } else {
      fail(`renderer bundle contains "${otherManifest.productName}" ${productNameHits} time(s)`)
    }
  }
}

const ELECTRON_BUNDLES = [
  { file: 'electron-main.mjs', markers: MAIN_MARKERS },
  {
    file: 'electron-preload.js',
    markers: {
      'ix-agency': [IPC_PREFIX['ix-agency']],
      quizverse: [IPC_PREFIX.quizverse],
      foundrly: [IPC_PREFIX.foundrly]
    }
  }
]

for (const { file, markers } of ELECTRON_BUNDLES) {
  const bundlePath = path.join(desktopRoot, 'dist', file)

  if (!fs.existsSync(bundlePath)) {
    fail(`dist/${file} missing — run \`DESKTOP_BRAND=${brandId} npm run build\` first`)
    continue
  }

  const text = fs.readFileSync(bundlePath, 'utf8')

  for (const otherBrand of otherBrands) {
    for (const marker of markers[otherBrand]) {
      if (text.includes(marker)) {
        fail(`dist/${file} leaks ${otherBrand} marker "${marker}" — brand-gates DCE regressed`)
      } else {
        ok(`dist/${file}: no ${otherBrand} marker "${marker}"`)
      }
    }
  }

  if (text.includes(IPC_PREFIX[brandId])) {
    ok(`dist/${file}: own IPC surface (${IPC_PREFIX[brandId]}*) present`)
  } else {
    fail(`dist/${file}: own IPC surface (${IPC_PREFIX[brandId]}*) missing — markers drifted or the brand surface was dropped`)
  }
}

const qvPreload = path.join(desktopRoot, 'dist', 'qv-webview-preload.js')

if (!fs.existsSync(qvPreload)) {
  ok(`dist/qv-webview-preload.js absent — obsolete webview bridge removed for ${brandId}`)
} else {
  fail(`dist/qv-webview-preload.js present in a ${brandId} build`)
}

if (failures.length > 0) {
  console.error(`[check-brand-separation] FAILED — ${failures.length} problem(s)`)
  process.exit(1)
}

console.log(`[check-brand-separation] OK — ${brandId} build is cleanly separated`)
