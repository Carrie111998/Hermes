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
const server = createServer((_request, response) => {
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
  response.end(FIXTURE)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const URL_ = `http://127.0.0.1:${server.address().port}/`

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

/** Install the engine and a tiny driver on the page. */
async function install() {
  await evaluate(`${bundled};
    window.__engine = eval(HermesPinBundle.pinEngineSource());
    window.__holder = {};
    window.__pins = function (command) { return window.__engine(document, window.__holder, command) };
    window.__seed = function (pins) {
      window.__holder.__hermesPinState = { armed: false, drag: null, pins: pins, seq: pins.length }
    };
    true`)
}

const centreOf = selector => evaluate(`(() => {
  const el = document.querySelector(${JSON.stringify(selector)})
  const r = el.getBoundingClientRect()
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 }
})()`)

try {
  await connect()
  await send('Runtime.enable')
  await install()
  check('engine installed in a real page', (await evaluate('typeof window.__pins')) === 'function')

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

  // ---- the reload that decides everything ---------------------------------

  const before = await evaluate(`JSON.stringify(window.__pins({ verb: 'state' }).pins)`)
  await send('Page.enable')
  await send('Page.reload', { ignoreCache: true })
  await wait(1200)
  await install()

  await evaluate(`window.__seed(${JSON.stringify(JSON.parse(before))})`)
  const after = await evaluate(`window.__pins({ verb: 'reattach' }).pins`)

  check('every pin came back after a genuine reload', after.length === 3, String(after.length))
  check('the element pin re-attached', after[0]?.orphaned === false, JSON.stringify(after[0]))
  check('it matched on the page\'s own id', after[0]?.matchedBy === 'selector', after[0]?.matchedBy)
  check('the comment survived', after[0]?.comment === 'too much space above this', after[0]?.comment)
  check('the ambiguous pin kept its row', after[1]?.orphaned === false, JSON.stringify(after[1]))
  check('the region pin is untouched', after[2]?.region?.w > 0 && after[2]?.orphaned === undefined)

  const repainted = await evaluate(`(() => {
    const host = document.getElementById('hermes-pin-host')
    return host ? host.shadowRoot.querySelectorAll('.pin').length : 0
  })()`)
  check('markers were redrawn after the reload', repainted === 3, String(repainted))

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
} catch (err) {
  failures += 1
  console.log(`  FAIL harness — ${err.message}`)
}

console.log(failures === 0 ? '\nall preview-pin browser checks passed\n' : `\n${failures} check(s) failed\n`)
cleanup()
process.exit(failures === 0 ? 0 : 1)
