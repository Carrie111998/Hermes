#!/usr/bin/env node
import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'

const root = path.resolve(new URL('../..', import.meta.url).pathname)
const desktopPackage = path.join(root, 'apps', 'desktop', 'package.json')
const requireFromDesktop = createRequire(desktopPackage)

function usage(exitCode = 0) {
  console.log(`Hermes Playwright capture helper

Usage:
  node tools/playwright-cli/capture.mjs <url> [--out .playwright-cli] [--name label] [--headed] [--wait-ms 0]

Writes local-only artifacts:
  <out>/page-<timestamp>.yml
  <out>/console-<timestamp>.log

Notes:
  - Artifacts are ignored by git; commit reusable scripts, not captured logs.
  - Playwright is resolved from apps/desktop devDependencies.
`)
  process.exit(exitCode)
}

const args = process.argv.slice(2)
if (args.includes('--help') || args.includes('-h')) usage(0)
const url = args.find(arg => !arg.startsWith('--'))
if (!url) usage(1)

function option(name, fallback) {
  const idx = args.indexOf(name)
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : fallback
}

const outDir = path.resolve(option('--out', '.playwright-cli'))
const name = option('--name', '')
const headed = args.includes('--headed')
const waitMs = Number(option('--wait-ms', '0')) || 0
const stamp = new Date().toISOString().replace(/[:.]/g, '-')
const safeName = name ? `${name.replace(/[^a-z0-9_-]+/gi, '-')}-` : ''

const { chromium } = requireFromDesktop('@playwright/test')

const browser = await chromium.launch({ headless: !headed })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const consoleLines = []
page.on('console', msg => {
  consoleLines.push(`${msg.type().toUpperCase()} ${msg.text()}`)
})
page.on('pageerror', err => {
  consoleLines.push(`PAGEERROR ${err.stack || err.message}`)
})

try {
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 })
  if (waitMs > 0) await page.waitForTimeout(waitMs)
  const snapshot = await page.locator('body').ariaSnapshot({ timeout: 10_000 }).catch(async () => {
    const title = await page.title().catch(() => '')
    const text = await page.locator('body').innerText({ timeout: 10_000 }).catch(() => '')
    return `- document: ${JSON.stringify(title)}\n- text: ${JSON.stringify(text.slice(0, 5000))}`
  })
  await mkdir(outDir, { recursive: true })
  const pagePath = path.join(outDir, `page-${safeName}${stamp}.yml`)
  const consolePath = path.join(outDir, `console-${safeName}${stamp}.log`)
  await writeFile(pagePath, snapshot, 'utf8')
  await writeFile(consolePath, consoleLines.join('\n'), 'utf8')
  console.log(JSON.stringify({ ok: true, url, pagePath, consolePath }, null, 2))
} finally {
  await browser.close()
}
