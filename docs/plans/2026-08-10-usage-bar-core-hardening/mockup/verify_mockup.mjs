// DOM assertions for the usage-bar mockup at 360px (parent acceptance B6).
// Run from apps/desktop: node ../../docs/plans/2026-08-10-usage-bar-core-hardening/mockup/verify_mockup.mjs
// Writes dom-assertions.json next to this script and exits non-zero on failure.
import { chromium } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import fs from 'node:fs'

const here = path.dirname(fileURLToPath(import.meta.url))
const pageUrl = q =>
  'file:///' + path.join(here, 'usage-bar-mockup.html').replaceAll('\\', '/') + q

const checks = []
const check = (name, ok, detail = '') => checks.push({ name, ok, detail })

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 360, height: 1150 } })

// --- popover scene, narrow ---
await page.goto(pageUrl('?scene=popover&theme=light&vp=narrow&full=1'))
await page.waitForTimeout(300)
const pop = await page.evaluate(() => {
  const el = document.querySelector('[data-scene="popover"] .popover')
  const body = document.body
  return {
    popScroll: el.scrollWidth,
    popClient: el.clientWidth,
    docScroll: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    bodyScroll: body.scrollWidth
  }
})
check('narrow popover: scrollWidth == clientWidth', pop.popScroll === pop.popClient, JSON.stringify(pop))
check('narrow popover: no page horizontal overflow', pop.docScroll <= 360, `docScroll=${pop.docScroll}`)

const focusables = await page.evaluate(() => {
  const scene = document.querySelector('[data-scene="popover"]')
  const els = [...scene.querySelectorAll('a[href], button')]
  return els.map(el => ({
    tag: el.tagName,
    text: (el.textContent || '').trim().slice(0, 40),
    tabindex: el.tabIndex,
    disabled: el.disabled,
    visible: el.getClientRects().length > 0
  }))
})
const badFocus = focusables.filter(f => f.visible && (f.tabindex < 0 || f.disabled))
check('popover: all visible actions are tabbable', badFocus.length === 0, JSON.stringify(badFocus))
check('popover: has real link/button actions', focusables.length >= 2, `count=${focusables.length}`)

const semantics = await page.evaluate(() => ({
  roleStatus: document.querySelectorAll('[data-scene="popover"] [role="status"]').length,
  roleAlert: document.querySelectorAll('[data-scene="popover"] [role="alert"]').length,
  officialCopy: document.body.textContent.includes('Official provider data'),
  leakedSource: document.body.textContent.includes('provider_reported')
}))
check('popover: role=status present for stale line', semantics.roleStatus >= 1, `count=${semantics.roleStatus}`)
check('popover: role=alert present for auth error', semantics.roleAlert >= 1, `count=${semantics.roleAlert}`)
check('popover: user-facing source copy', semantics.officialCopy && !semantics.leakedSource, JSON.stringify(semantics))

// progress bar fill == "xx% left" text
const bars = await page.evaluate(() => {
  const rows = [...document.querySelectorAll('[data-scene="popover"] .stack')]
  const out = []
  for (const row of rows) {
    const label = row.querySelector('.row-center .fg.tabular')
    const bar = row.querySelector('.progress > i')
    if (label && bar) {
      const m = label.textContent.match(/(\d+)% left/)
      const w = bar.style.width.match(/(\d+)%/)
      if (m && w) out.push({ text: m[1], fill: w[1], match: m[1] === w[1] })
    }
  }
  return out
})
check('popover: every account-limit bar fill equals its "% left" text', bars.length > 0 && bars.every(b => b.match), JSON.stringify(bars))

// --- command center scene, narrow ---
await page.goto(pageUrl('?scene=command-center&theme=light&vp=narrow'))
await page.waitForTimeout(300)
const cc = await page.evaluate(() => {
  const el = document.querySelector('[data-scene="command-center"] .overlay')
  const text = document.body.textContent
  return {
    overlayScroll: el.scrollWidth,
    overlayClient: el.clientWidth,
    docScroll: document.documentElement.scrollWidth,
    hasTask: text.includes('By task'),
    hasCoding: text.includes('coding')
  }
})
check('narrow command-center: no horizontal overflow', cc.overlayScroll === cc.overlayClient && cc.docScroll <= 360, JSON.stringify(cc))
check('command-center: Task dimension present', cc.hasTask && cc.hasCoding, JSON.stringify({ hasTask: cc.hasTask, hasCoding: cc.hasCoding }))

// --- palette scene: real focusable rows ---
await page.goto(pageUrl('?scene=palette&theme=light'))
await page.waitForTimeout(200)
const palette = await page.evaluate(() => {
  const items = [...document.querySelectorAll('[data-scene="palette"] .palette-item')]
  return {
    count: items.length,
    allButtons: items.every(el => el.tagName === 'BUTTON'),
    allTabbable: items.every(el => el.tabIndex >= 0 && !el.disabled),
    listbox: !!document.querySelector('[data-scene="palette"] [role="listbox"]')
  }
})
check('palette: rows are real focusable buttons in a listbox', palette.count >= 3 && palette.allButtons && palette.allTabbable && palette.listbox, JSON.stringify(palette))

// --- Esc keyboard path (real, mockup scope) ---
await page.goto(pageUrl('?scene=popover&theme=light'))
await page.waitForTimeout(200)
const esc = await page.evaluate(async () => {
  const pop = document.querySelector('[data-scene="popover"] .popover')
  const trigger = document.querySelector('[data-scene="popover"] [data-popover-trigger]')
  const before = pop.style.display !== 'none'
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  await new Promise(r => setTimeout(r, 50))
  return {
    before,
    afterHidden: pop.style.display === 'none',
    ariaExpanded: trigger.getAttribute('aria-expanded'),
    focusReturned: document.activeElement === trigger
  }
})
check('popover: Escape closes and returns focus to trigger', esc.before && esc.afterHidden && esc.ariaExpanded === 'false' && esc.focusReturned, JSON.stringify(esc))

await browser.close()

const failed = checks.filter(c => !c.ok)
const report = {
  generated_at: new Date().toISOString(),
  viewport_narrow: '360px',
  total: checks.length,
  passed: checks.length - failed.length,
  checks
}
fs.writeFileSync(path.join(here, 'dom-assertions.json'), JSON.stringify(report, null, 2))
console.log(JSON.stringify(report, null, 2))
if (failed.length) {
  console.error(`FAILED: ${failed.length} assertion(s)`)
  process.exit(1)
}
console.log('ALL DOM ASSERTIONS PASSED')
