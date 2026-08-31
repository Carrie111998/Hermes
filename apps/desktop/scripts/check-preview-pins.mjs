/**
 * The pin engine in a real browser, driven by real input.
 *
 * jsdom carries the unit tests, but it has no layout: every
 * getBoundingClientRect is zero, so it cannot answer the questions that decide
 * whether pins actually work — does the overlay land on the element, does a
 * shadow root really keep page CSS out, does a pin come back after a genuine
 * reload rather than an innerHTML swap.
 *
 * So: bundle the engine with esbuild, load a fixture in headless Chromium, and
 * drive it with CDP `Input.dispatchMouseEvent`, which produces TRUSTED input at
 * real coordinates — the same distinction preview-act.ts draws between
 * sendInputEvent and script-dispatched events.
 *
 *   node scripts/check-preview-pins.mjs [--chrome /usr/bin/chromium-browser]
 */

import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const args = process.argv.slice(2)
const flag = (name, fallback) => {
  const index = args.indexOf(`--${name}`)
  return index === -1 ? fallback : args[index + 1]
}
const CHROME = flag('chrome', '/usr/bin/chromium-browser')
const DEBUG_PORT = Number(flag('debug-port', 9355))
const ROOT = fileURLToPath(new URL('..', import.meta.url))

let failures = 0
const check = (label, condition, detail = '') => {
  if (condition) console.log(`  ok   ${label}`)
  else {
    failures += 1
    const text = typeof detail === 'string' ? detail : JSON.stringify(detail)
    console.log(`  FAIL ${label}${text ? ` — ${text}` : ''}`)
  }
}
const wait = ms => new Promise(resolve => setTimeout(resolve, ms))

// A layout with real geometry: two identical rows, so the ordinal rung is
// exercised, plus a hostile stylesheet aimed at the overlay's own class names.
const FIXTURE = `<!doctype html>
<html><head><meta charset="utf-8"><title>Pin fixture</title><style>
  body{margin:0;font:15px system-ui;padding:24px}
  #hero{height:120px;background:#eee;margin-bottom:20px}
  .row{display:flex;gap:8px;align-items:center;margin:10px 0}
  button{padding:8px 14px}
  /* An app that happens to style these names must not reach the review tools. */
  .pin,.bubble,.hl{display:none !important;visibility:hidden !important}
</style></head><body>
  <h1 id="title">Dashboard</h1>
  <div id="hero">chart goes here</div>
  <div class="row"><span>Alpha</span><button class="act">Edit</button></div>
  <div class="row"><span>Beta</span><button class="act">Edit</button></div>
  <button id="save" style="margin-top:30px">Save changes</button>
  <p style="margin-top:40px"><a id="go" href="/second.html">Go to the second page</a></p>
</body></html>`

// A second page, so the walk between pages can be tested for real: the pin book
// must not spill page one's comments onto page two.
const SECOND = `<!doctype html>
<html><head><meta charset="utf-8"><title>Second</title><style>
  body{margin:0;font:15px system-ui;padding:24px}
</style></head><body>
  <h1 id="second-title">Second page</h1>
  <button id="publish" style="margin-top:20px">Publish</button>
</body></html>`

// ---- bundle the engine -----------------------------------------------------

// Dependencies are hoisted to the workspace root, so the local bin dir holds
// only the few packages npm could not hoist (electron, parser).
const esbuildBin = existsSync(join(ROOT, 'node_modules/.bin/esbuild'))
  ? join(ROOT, 'node_modules/.bin/esbuild')
  : join(ROOT, '../../node_modules/.bin/esbuild')

const bundle = spawn(
  esbuildBin,
  [
    join(ROOT, 'src/lib/preview-pins/pin-in-page.ts'),
    '--bundle',
    '--format=iife',
    '--global-name=HermesPinBundle',
    '--platform=browser'
  ],
  { stdio: ['ignore', 'pipe', 'pipe'] }
)
let bundled = ''
let bundleErr = ''
bundle.stdout.on('data', chunk => { bundled += chunk })
bundle.stderr.on('data', chunk => { bundleErr += chunk })
await new Promise(resolve => bundle.on('close', resolve))
if (!bundled) {
  console.log(`\nesbuild failed:\n${bundleErr}`)
  process.exit(1)
}

// ---- serve + launch --------------------------------------------------------

const profileDir = mkdtempSync(join(tmpdir(), 'hermes-pins-'))
const server = createServer((request, response) => {
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
  response.end(request.url?.startsWith('/second') ? SECOND : FIXTURE)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const URL_ = `http://127.0.0.1:${server.address().port}/`
const ORIGIN = `http://127.0.0.1:${server.address().port}`

console.log(`\ncheck-preview-pins — ${CHROME}\n`)

const chrome = spawn(CHROME, [
  '--headless=new',
  `--user-data-dir=${profileDir}`,
  `--remote-debugging-port=${DEBUG_PORT}`,
  '--no-first-run',
  '--no-default-browser-check',
  '--window-size=1000,900',
  URL_
], { stdio: 'ignore' })

const cleanup = () => {
  try { chrome.kill('SIGTERM') } catch { /* gone */ }
  try { server.close() } catch { /* closed */ }
  try { rmSync(profileDir, { recursive: true, force: true }) } catch { /* best effort */ }
}
process.on('exit', cleanup)

// ---- CDP -------------------------------------------------------------------

let socket = null
let nextId = 1
const pending = new Map()

async function connect() {
  const deadline = Date.now() + 30_000
  while (Date.now() < deadline) {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json()
      const page = targets.find(target => target.type === 'page' && target.url.startsWith('http'))
      if (page) {
        socket = new WebSocket(page.webSocketDebuggerUrl)
        await new Promise((resolve, reject) => {
          socket.onopen = resolve
          socket.onerror = reject
        })
        socket.onmessage = event => {
          const message = JSON.parse(event.data)
          const waiter = pending.get(message.id)
          if (!waiter) return
          pending.delete(message.id)
          message.error ? waiter.reject(new Error(JSON.stringify(message.error))) : waiter.resolve(message.result)
        }
        return
      }
    } catch { /* still booting */ }
    await wait(400)
  }
  throw new Error('no page target appeared')
}

function send(method, params = {}) {
  const id = nextId++
  return new Promise((resolve, reject) => {
    pending.set(id, { reject, resolve })
    socket.send(JSON.stringify({ id, method, params }))
  })
}

async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { awaitPromise: true, expression, returnByValue: true })
  if (result.result?.subtype === 'error') throw new Error(result.result.description)
  return result.result?.value
}

/** A real press-and-release at real coordinates — trusted input, not a
 *  dispatched MouseEvent the page could tell apart. */
async function realClick(x, y, { steps = 3 } = {}) {
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, buttons: 0 })
  for (let step = 1; step <= steps; step += 1) await wait(10)
  await send('Input.dispatchMouseEvent', { button: 'left', buttons: 1, clickCount: 1, type: 'mousePressed', x, y })
  await send('Input.dispatchMouseEvent', { button: 'left', buttons: 0, clickCount: 1, type: 'mouseReleased', x, y })
  await wait(80)
}

async function realDrag(x0, y0, x1, y1) {
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: x0, y: y0, buttons: 0 })
  await send('Input.dispatchMouseEvent', { button: 'left', buttons: 1, clickCount: 1, type: 'mousePressed', x: x0, y: y0 })
  await send('Input.dispatchMouseEvent', { buttons: 1, type: 'mouseMoved', x: (x0 + x1) / 2, y: (y0 + y1) / 2 })
  await send('Input.dispatchMouseEvent', { buttons: 1, type: 'mouseMoved', x: x1, y: y1 })
  await send('Input.dispatchMouseEvent', { button: 'left', buttons: 0, clickCount: 1, type: 'mouseReleased', x: x1, y: y1 })
  await wait(80)
}

/**
 * Wait until the document that will still be here after installing has loaded.
 *
 * Evaluating into a context that is about to be replaced by the real document
 * silently loses every global, which shows up much later as "__pins is not a
 * function" with no error to explain it.
 */
async function settled() {
  const deadline = Date.now() + 15_000
  while (Date.now() < deadline) {
    try {
      const where = await evaluate('document.readyState + "|" + location.origin')
      // The origin matters as much as the state: about:blank reaches
      // "complete" first and is then replaced, taking every global with it.
      if (where === `complete|${ORIGIN}`) return
    } catch { /* context swapping under us */ }
    await wait(150)
  }
}

/** Install the engine and a tiny driver on the page. */
async function install() {
  await evaluate(`try { ${bundled};
    window.__engine = eval(HermesPinBundle.pinEngineSource());
    window.__holder = {};
    window.__pins = function (command) { return window.__engine(document, window.__holder, command) };
    window.__seed = function (pins) {
      // Mirrors seedScript in preview-pins.ts, filter and all — the rule that
      // stops one page's comments from being replayed onto another is only
      // worth testing in the form the app actually ships.
      var here = String(location.href).replace(/#$/, '');
      var seed = pins.filter(function (pin) {
        return !pin.pageUrl || String(pin.pageUrl).replace(/#$/, '') === here;
      });
      window.__holder.__hermesPinState = {
        armed: false, drag: null, hidden: false, pending: [],
        pins: seed, seq: seed.length, shotData: {}
      }
    };
    true } catch (err) { window.__installError = err && err.message; false }`)
}

/**
 * Install, then prove it took.
 *
 * A context swap between the evaluate and the next call is silent — the globals
 * simply are not there, and it surfaces later as an unexplained missing
 * function. Verify and retry rather than trusting one shot.
 */
async function installed() {
  for (let attempt = 0; attempt < 6; attempt += 1) {
    await settled()
    await install()

    try {
      if ((await evaluate('typeof window.__pins')) === 'function') return true
    } catch { /* swapped mid-check */ }

    await wait(250)
  }

  return false
}

const centreOf = selector => evaluate(`(() => {
  const el = document.querySelector(${JSON.stringify(selector)})
  const r = el.getBoundingClientRect()
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 }
})()`)

try {
  await connect()
  await send('Runtime.enable')
  check(
    'engine installed in a real page',
    await installed(),
    await evaluate('String(window.__installError || "no error recorded")')
  )

  await evaluate(`window.__pins({ verb: 'arm' })`)
  check('armed', (await evaluate(`window.__pins({ verb: 'state' }).armed`)) === true)

  // ---- placing with trusted input -----------------------------------------

  const save = await centreOf('#save')
  await realClick(save.x, save.y)
  let pins = await evaluate(`window.__pins({ verb: 'state' }).pins`)
  check('a real click placed a pin', pins.length === 1, JSON.stringify(pins))
  check('the pin captured the right element', pins[0]?.anchor?.selector === '#save', pins[0]?.anchor?.selector)

  check(
    'placing did not activate the page control',
    (await evaluate(`document.activeElement && document.activeElement.id !== 'save'`)) === true
  )

  // ---- the comment bubble sits beside its element, not on top of it -------

  const bubbleVsTarget = await evaluate(`(() => {
    const node = document.getElementById('hermes-pin-host').shadowRoot.querySelector('.bubble')
    if (!node) return null
    const b = node.getBoundingClientRect()
    const t = document.getElementById('save').getBoundingClientRect()
    const overlaps = b.left < t.right && b.right > t.left && b.top < t.bottom && b.bottom > t.top
    return {
      fitsHorizontally: b.left >= 0 && b.right <= innerWidth + 1,
      fitsVertically: b.top >= 0 && b.bottom <= innerHeight + 1,
      head: (node.querySelector('.head span') || {}).textContent || '',
      overlaps,
      width: Math.round(b.width)
    }
  })()`)

  check('the bubble opened on placement', bubbleVsTarget !== null)
  check(
    'it does not cover the element being commented on',
    bubbleVsTarget && !bubbleVsTarget.overlaps,
    JSON.stringify(bubbleVsTarget)
  )
  check('it stays inside the viewport', bubbleVsTarget && bubbleVsTarget.fitsHorizontally && bubbleVsTarget.fitsVertically, JSON.stringify(bubbleVsTarget))
  check('it names the element it belongs to', bubbleVsTarget?.head === 'Save changes', bubbleVsTarget?.head)

  // A narrow phone viewport must not push it off the side.
  await send('Emulation.setDeviceMetricsOverride', { deviceScaleFactor: 0, height: 700, mobile: false, width: 360 })
  await wait(300)
  await evaluate(`(() => {
    const node = document.getElementById('hermes-pin-host').shadowRoot.querySelector('.bubble')
    node.dispatchEvent(new Event('x'))
    return true
  })()`)
  const narrow = await evaluate(`(() => {
    const node = document.getElementById('hermes-pin-host').shadowRoot.querySelector('.bubble')
    const b = node.getBoundingClientRect()
    return { fits: b.width <= innerWidth - 16, viewport: innerWidth, width: Math.round(b.width) }
  })()`)
  check('it narrows with a phone-width viewport', narrow?.fits, JSON.stringify(narrow))
  await send('Emulation.clearDeviceMetricsOverride')
  await wait(200)

  await evaluate(`document.getElementById('hermes-pin-host').shadowRoot.querySelector('.bubble .go').click(); true`)

  await evaluate(`window.__pins({ comment: 'too much space above this', id: ${JSON.stringify(pins[0].id)}, verb: 'comment' })`)

  // ---- overlay in the real render tree ------------------------------------

  const marker = await evaluate(`(() => {
    const host = document.getElementById('hermes-pin-host')
    const node = host.shadowRoot.querySelector('.pin')
    if (!node) return null
    const r = node.getBoundingClientRect()
    const style = getComputedStyle(node)
    return { display: style.display, h: r.height, visibility: style.visibility, x: r.left, y: r.top }
  })()`)
  check('a marker was drawn', marker !== null, 'no .pin in the shadow root')
  check(
    'page CSS could not hide the overlay',
    marker && marker.display !== 'none' && marker.visibility !== 'hidden',
    JSON.stringify(marker)
  )
  check(
    'the marker sits on its element',
    marker && Math.abs(marker.x - (save.x - 11)) < 60 && Math.abs(marker.y - (save.y - 40)) < 60,
    `marker ${JSON.stringify(marker)} vs element centre ${JSON.stringify(save)}`
  )
  check(
    'the host is invisible to page selectors',
    (await evaluate(`document.querySelectorAll('.pin, .bubble, .hl').length`)) === 0
  )

  // ---- ambiguity: two identical buttons -----------------------------------

  const second = await evaluate(`(() => {
    const el = document.querySelectorAll('.act')[1]
    const r = el.getBoundingClientRect()
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 }
  })()`)
  await realClick(second.x, second.y)
  pins = await evaluate(`window.__pins({ verb: 'state' }).pins`)
  check('pinned the second of two identical buttons', pins.length === 2, String(pins.length))
  check('recorded which one it was', pins[1]?.anchor?.ordinal === 1, JSON.stringify(pins[1]?.anchor))

  // ---- region pin ----------------------------------------------------------

  const hero = await centreOf('#hero')
  await realDrag(hero.x - 80, hero.y - 30, hero.x + 80, hero.y + 30)
  pins = await evaluate(`window.__pins({ verb: 'state' }).pins`)
  check('a drag made a region pin', pins[2]?.kind === 'region', JSON.stringify(pins[2]))
  check('the region has real extent', (pins[2]?.region?.w ?? 0) > 0 && (pins[2]?.region?.h ?? 0) > 0)

  // ---- attaching an image, through the real canvas path -------------------

  // jsdom has no canvas and never fires Image.onload, so the downscale, the
  // thumbnail and the strip only mean anything here.
  const title = await centreOf('#title')
  await realClick(title.x, title.y)

  const pasted = await evaluate(`(async () => {
    const canvas = document.createElement('canvas')
    canvas.width = 2000
    canvas.height = 1200
    const ctx = canvas.getContext('2d')
    ctx.fillStyle = '#2f4fd0'
    ctx.fillRect(0, 0, 2000, 1200)
    ctx.fillStyle = '#ffffff'
    ctx.fillRect(120, 120, 700, 500)
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'))
    const file = new File([blob], 'mockup.png', { type: 'image/png' })
    const transfer = new DataTransfer()
    transfer.items.add(file)
    const area = document.getElementById('hermes-pin-host').shadowRoot.querySelector('.bubble textarea')
    if (!area) return { error: 'no bubble textarea' }
    const event = new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: transfer })
    area.dispatchEvent(event)
    return { original: blob.size, prevented: event.defaultPrevented }
  })()`)

  check('the bubble was open to paste into', !pasted?.error, pasted?.error)
  check('the paste was swallowed, so no filename lands in the comment', pasted?.prevented === true)

  // FileReader, then two Image decodes and two canvas encodes.
  let shots = []
  for (let tries = 0; tries < 40 && !shots.length; tries += 1) {
    await wait(100)
    const state = await evaluate(`window.__pins({ verb: 'state' }).pins`)
    shots = state[3]?.shots ?? []
  }

  check('a pasted image became an attachment on the pin', shots.length === 1, JSON.stringify(shots).slice(0, 200))
  check('it was downscaled to the bound', shots[0]?.w === 1400 && shots[0]?.h === 840, JSON.stringify(shots[0] && { h: shots[0].h, w: shots[0].w }))
  check('the thumbnail is small enough to ride in every report', (shots[0]?.thumb?.length ?? 1e9) < 12_000, String(shots[0]?.thumb?.length))

  const holding = await evaluate(`window.__pins({ verb: 'state' })`)
  check('the page announces what it is still holding', holding.pendingShots?.length === 1, JSON.stringify(holding.pendingShots))
  check('an ordinary read carries no image bytes', JSON.stringify(holding.pins).length < 40_000, String(JSON.stringify(holding.pins).length))

  const taken = await evaluate(`window.__pins({ id: ${JSON.stringify(shots[0]?.id ?? '')}, verb: 'take' })`)
  check('the bytes come out on demand', typeof taken.shot === 'string' && taken.shot.startsWith('data:image/jpeg'), String(taken.shot).slice(0, 40))
  check('the downscale is worth doing', taken.shot.length < (pasted.original ?? 0), `${taken.shot?.length} vs ${pasted?.original}`)
  check('nothing is left behind in the page', taken.pendingShots?.length === 0, JSON.stringify(taken.pendingShots))
  check('asking twice does not resurrect them', (await evaluate(`window.__pins({ id: ${JSON.stringify(shots[0]?.id ?? '')}, verb: 'take' }).shot`)) === null)

  const bubbleBox = await evaluate(`(() => {
    const node = document.getElementById('hermes-pin-host').shadowRoot.querySelector('.bubble')
    if (!node) return null
    const r = node.getBoundingClientRect()
    return { bottom: r.bottom, height: r.height, right: r.right, strip: node.querySelectorAll('.strip img').length }
  })()`)
  check('the thumbnail is shown in the bubble', bubbleBox?.strip === 1, JSON.stringify(bubbleBox))
  check(
    'the bubble stayed on screen after growing',
    bubbleBox && bubbleBox.bottom <= 900 && bubbleBox.right <= 1000,
    JSON.stringify(bubbleBox)
  )

  // The comment field's scrollbar is the guest page's, which on a plain page is
  // Chromium's chunky legacy bar with stepper arrows — ~15px of another app's
  // furniture inside a 304px bubble. Only a real browser can say how wide it
  // actually came out; jsdom has no scrollbars at all.
  const gutter = await evaluate(`(() => {
    const area = document.getElementById('hermes-pin-host').shadowRoot.querySelector('.bubble textarea')
    if (!area) return null
    const was = area.value
    area.value = ('a long line that has to wrap and wrap again ').repeat(24)
    const style = getComputedStyle(area)
    const width = area.offsetWidth - area.clientWidth -
      parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth)
    const overflows = area.scrollHeight > area.clientHeight
    area.value = was
    return { overflows, width }
  })()`)
  check('the comment field does overflow, so there is a scrollbar to judge', gutter?.overflows === true, JSON.stringify(gutter))
  check('its scrollbar is a slim one, not the page default', gutter && gutter.width > 0 && gutter.width <= 8, JSON.stringify(gutter))

  await evaluate(`document.getElementById('hermes-pin-host').shadowRoot.querySelector('.bubble .go').click(); true`)

  // ---- the reload that decides everything ---------------------------------

  const before = await evaluate(`JSON.stringify(window.__pins({ verb: 'state' }).pins)`)
  await send('Page.enable')
  await send('Page.reload', { ignoreCache: true })
  await wait(400)
  await installed()

  await evaluate(`window.__seed(${JSON.stringify(JSON.parse(before))})`)
  const after = await evaluate(`window.__pins({ verb: 'reattach' }).pins`)

  check('every pin came back after a genuine reload', after.length === 4, String(after.length))
  check('the element pin re-attached', after[0]?.orphaned === false, JSON.stringify(after[0]))
  check('it matched on the page\'s own id', after[0]?.matchedBy === 'selector', after[0]?.matchedBy)
  check('the comment survived', after[0]?.comment === 'too much space above this', after[0]?.comment)
  check('the ambiguous pin kept its row', after[1]?.orphaned === false, JSON.stringify(after[1]))
  check('the region pin is untouched', after[2]?.region?.w > 0 && after[2]?.orphaned === undefined)

  const repainted = await evaluate(`(() => {
    const host = document.getElementById('hermes-pin-host')
    return host ? host.shadowRoot.querySelectorAll('.pin').length : 0
  })()`)
  check('markers were redrawn after the reload', repainted === 4, String(repainted))
  check('the thumbnail rode along through the reload', (after[3]?.shots?.length ?? 0) === 1, JSON.stringify(after[3]?.shots?.length))

  // ---- the element genuinely goes away ------------------------------------

  await evaluate(`document.getElementById('save').remove(); true`)
  const orphaned = await evaluate(`window.__pins({ verb: 'reattach' }).pins`)
  check('a removed element orphans its pin instead of stealing a neighbour', orphaned[0]?.orphaned === true,
    JSON.stringify(orphaned[0]))
  check('the orphan kept the user\'s words', orphaned[0]?.comment === 'too much space above this')

  // ---- disarming returns the page ------------------------------------------

  await evaluate(`window.__pins({ verb: 'disarm' })`)
  const cursor = await evaluate(`document.documentElement.style.cursor`)
  check('disarming restores the cursor', cursor === '', `cursor is "${cursor}"`)
  const hostStyle = await evaluate(`document.getElementById('hermes-pin-host').getAttribute('style')`)
  check('the overlay stops eating clicks', hostStyle.includes('pointer-events:none'), hostStyle)

  // ---- closing the panel gives the page back -------------------------------
  //
  // The reported bug, reproduced the way it was hit: annotate, hide the panel,
  // click a link. Armed, the click is ours; hidden, it is the page's.

  const link = await centreOf('#go')
  await evaluate(`window.__pins({ verb: 'arm' })`)
  const pinsBeforeLink = (await evaluate(`window.__pins({ verb: 'state' }).pins`)).length
  await realClick(link.x, link.y)
  await wait(300)

  check(
    'armed, a click on a link is a comment and not a navigation',
    (await evaluate(`location.pathname`)) === '/',
    await evaluate(`location.pathname`)
  )
  check(
    'and it left a pin on the link',
    (await evaluate(`window.__pins({ verb: 'state' }).pins`)).length === pinsBeforeLink + 1
  )

  const afterHide = await evaluate(`window.__pins({ verb: 'hide' })`)
  check('hiding disarms', afterHide.armed === false && afterHide.hidden === true, JSON.stringify({ armed: afterHide.armed, hidden: afterHide.hidden }))
  check(
    'hiding takes the markers down',
    (await evaluate(`document.getElementById('hermes-pin-host').shadowRoot.querySelectorAll('.pin').length`)) === 0
  )
  check('hiding keeps the comments', afterHide.pins.length === pinsBeforeLink + 1, String(afterHide.pins.length))

  const carried = await evaluate(`JSON.stringify(window.__pins({ verb: 'state' }).pins)`)
  await realClick(link.x, link.y)
  await wait(900)

  check(
    'hidden, the same click navigates',
    (await evaluate(`location.pathname`)) === '/second.html',
    await evaluate(`location.pathname`)
  )

  // ---- the second page keeps its own comments ------------------------------

  await installed()
  await evaluate(`window.__seed(${JSON.stringify(JSON.parse(carried))})`)
  const onSecond = await evaluate(`window.__pins({ verb: 'reattach' }).pins`)
  check(
    'page one\'s comments did not follow us here',
    onSecond.length === 0,
    JSON.stringify(onSecond.map(pin => pin.target))
  )

  await evaluate(`window.__pins({ verb: 'arm' })`)
  const publish = await centreOf('#publish')
  await realClick(publish.x, publish.y)
  const secondPins = await evaluate(`window.__pins({ verb: 'state' }).pins`)
  check('a comment can be placed on the second page', secondPins.length === 1, String(secondPins.length))
  check(
    'and it knows which page it belongs to',
    secondPins[0]?.pageUrl?.endsWith('/second.html'),
    secondPins[0]?.pageUrl
  )

  // Both pages together is what "Attach to chat" sends.
  const mixed = JSON.parse(carried).concat(secondPins)
  check('the review now spans two pages', new Set(mixed.map(pin => pin.pageUrl)).size === 2, String(new Set(mixed.map(pin => pin.pageUrl)).size))
} catch (err) {
  failures += 1
  console.log(`  FAIL harness — ${err.message}`)
}

console.log(failures === 0 ? '\nall preview-pin browser checks passed\n' : `\n${failures} check(s) failed\n`)
cleanup()
process.exit(failures === 0 ? 0 : 1)
