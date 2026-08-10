// Screenshot the usage-bar mockup scenes with the system Chrome channel.
// Run from apps/desktop: node ../../docs/plans/2026-08-10-usage-bar-core-hardening/mockup/shoot.mjs
import { chromium } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const page_url = scene =>
  'file:///' + path.join(here, 'usage-bar-mockup.html').replaceAll('\\', '/') + scene

const shots = [
  ['index', '?scene=index&theme=light', 1280, 800],
  ['statusbar-light', '?scene=statusbar&theme=light', 1280, 800],
  ['statusbar-dark', '?scene=statusbar&theme=dark', 1280, 800],
  ['popover-light', '?scene=popover&theme=light&full=1', 1280, 1150],
  ['popover-dark', '?scene=popover&theme=dark&full=1', 1280, 1150],
  ['popover-narrow-light', '?scene=popover&theme=light&vp=narrow&full=1', 360, 1150],
  ['command-center-light', '?scene=command-center&theme=light', 1280, 800],
  ['command-center-dark', '?scene=command-center&theme=dark', 1280, 800],
  ['command-center-narrow-light', '?scene=command-center&theme=light&vp=narrow', 360, 640],
  ['palette-light', '?scene=palette&theme=light', 1280, 800],
  ['palette-dark', '?scene=palette&theme=dark', 1280, 800]
]

const browser = await chromium.launch({ channel: 'chrome', headless: true })
for (const [name, query, width, height] of shots) {
  const page = await browser.newPage({ viewport: { width, height } })
  await page.goto(page_url(query))
  await page.waitForTimeout(300)
  await page.screenshot({ path: path.join(here, `${name}.png`) })
  console.log('shot', name)
  await page.close()
}
await browser.close()
console.log('done')
