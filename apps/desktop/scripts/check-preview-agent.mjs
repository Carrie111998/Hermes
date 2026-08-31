/**
 * The agent's cursor and the act engine, in a real browser.
 *
 * The question this answers is the one jsdom structurally cannot: the agent is
 * supposed to have a pointer of its OWN, so a person can keep using their mouse
 * while it works. jsdom has no layout and no compositor — every rect is zero
 * and nothing is ever painted — so "does a second cursor appear, does it travel
 * to the thing being acted on, and does it survive a page that is actively
 * hostile to it" can only be asked here.
 *
 * It also pins down what the agent's cursor is NOT. It is a DOM element inside
 * a shadow root, never the OS pointer: the checks below assert that acting does
 * not move the real mouse, because that is the property the whole arrangement
 * rests on and it would break silently.
 *
 *   node scripts/check-preview-agent.mjs [--chrome /usr/bin/chromium-browser]
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
const DEBUG_PORT = Number(flag('debug-port', 9357))
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

// Real geometry, a hover-only menu (the case script-dispatched clicks miss),
// and a stylesheet that tries to kill anything the overlay might be made of.
const FIXTURE = `<!doctype html>
<html><head><meta charset="utf-8"><title>Agent fixture</title><style>
  body{margin:0;font:15px system-ui;padding:24px}
  button{padding:8px 14px}
  #menu{position:relative;margin:40px 0}
  #menu .items{display:none;position:absolute;top:100%;left:0;background:#fff;border:1px solid #ccc}
  #menu:hover .items{display:block}
  #menu .items button{display:block;width:160px}
  #far{margin-top:220px}
  /* Hostile: an app that styles these names must not reach the agent's chrome. */
  svg,.cursor,.mark,.shell,.skin{display:none !important;opacity:0 !important}
</style></head><body>
  <h1 id="title">Agent fixture</h1>
  <button id="save">Save changes</button>
  <div id="menu"><button id="opener">Open menu</button>
    <div class="items"><button id="hidden-item">Only when hovered</button></div>
  </div>
  <input id="email" placeholder="email">
  <button id="far">Far away</button>
  <p id="out">nothing yet</p>
  <script>
    document.getElementById('save').addEventListener('click', () => {
      document.getElementById('out').textContent = 'saved'
    })
    document.getElementById('hidden-item').addEventListener('click', () => {
      document.getElementById('out').textContent = 'picked from menu'
    })
  </script>
</body></html>`

// ---- bundle the overlay ----------------------------------------------------

const esbuildBin = existsSync(join(ROOT, 'node_modules/.bin/esbuild'))
  ? join(ROOT, 'node_modules/.bin/esbuild')
  : join(ROOT, '../../node_modules/.bin/esbuild')

async function bundleOf(entry, globalName) {
  const proc = spawn(
    esbuildBin,
    [join(ROOT, entry), '--bundle', '--format=iife', `--global-name=${globalName}`, '--platform=browser'],
    { stdio: ['ignore', 'pipe', 'pipe'] }
  )
  let out = ''
  let err = ''
  proc.stdout.on('data', chunk => { out += chunk })
  proc.stderr.on('data', chunk => { err += chunk })
  await new Promise(resolve => proc.on('close', resolve))
  if (!out) {
    console.log(`\nesbuild failed for ${entry}:\n${err}`)
    process.exit(1)
  }
  return out
}

const watchBundle = await bundleOf('src/lib/preview-act/watch-in-page.ts', 'HermesWatchBundle')
const actBundle = await bundleOf('src/lib/preview-act/act-in-page.ts', 'HermesActBundle')

// ---- serve + launch --------------------------------------------------------

const profileDir = mkdtempSync(join(tmpdir(), 'hermes-agent-'))
const server = createServer((request, response) => {
  response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
  response.end(FIXTURE)
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
const PORT = server.address().port
const URL_ = `http://127.0.0.1:${PORT}/`
const ORIGIN = `http://127.0.0.1:${PORT}`

console.log(`\ncheck-preview-agent — ${CHROME}\n`)

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
const consoleSeen = []

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
          // Chromium's own view of the console, which is what the pane's
          // `console-message` listener is fed from.
          if (message.method === 'Runtime.consoleAPICalled') {
            consoleSeen.push(message.params.type)
          }
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

async function settled() {
  const deadline = Date.now() + 15_000
  while (Date.now() < deadline) {
    try {
      const where = await evaluate('document.readyState + "|" + location.origin')
      if (where === `complete|${ORIGIN}`) return
    } catch { /* context swapping under us */ }
    await wait(150)
  }
  throw new Error('page never settled')
}

/** Real trusted input at real coordinates — what `sendInputEvent` produces in
 *  the app, and what a script-dispatched MouseEvent is not. */
async function realMove(x, y) {
  await send('Input.dispatchMouseEvent', { buttons: 0, type: 'mouseMoved', x, y })
  await wait(30)
}

async function realClick(x, y) {
  await realMove(x, y)
  await send('Input.dispatchMouseEvent', { button: 'left', buttons: 1, clickCount: 1, type: 'mousePressed', x, y })
  await send('Input.dispatchMouseEvent', { button: 'left', buttons: 0, clickCount: 1, type: 'mouseReleased', x, y })
  await wait(90)
}

await connect()
await send('Runtime.enable')
await settled()

// ---- install ---------------------------------------------------------------

await evaluate(`${watchBundle}; void 0`)
await evaluate(`${actBundle}; void 0`)

const installed = await evaluate(`
  (function () {
    var w = window
    var watch = (w.HermesWatchBundle && w.HermesWatchBundle.watchInPage) || null
    var act = (w.HermesActBundle && w.HermesActBundle.actInPage) || null
    if (!watch || !act) return { ok: false, watch: !!watch, act: !!act }
    w.__holder = {}
    w.__watch = watch
    w.__act = act
    return { ok: true }
  })()
`)
check('the overlay and act engines load in a real page', installed?.ok === true, installed)

// ---- 1. the agent has a cursor, and it is not yours -------------------------

console.log('\nthe agent’s pointer')

// `aim` is the stage that means "the agent is looking at this" — it boxes the
// target and sends the cursor to it.
async function aimAt(selector) {
  return evaluate(`
    (function () {
      // The real pipeline: 'locate' parks the target on the holder as
      // \`aimed\`, and the overlay's 'aim' stage is what reads it. Setting a
      // rect directly would test a path the agent never takes.
      var located = window.__act(document, window.__holder, {
        kind: 'locate',
        selector: ${JSON.stringify(selector)}
      })
      if (!located || !located.success) return { found: false, located: located }
      window.__watch(document, window.__holder, 'aim')
      return { found: true, x: located.point && located.point.x, y: located.point && located.point.y }
    })()
  `)
}

const savePoint = await aimAt('#save')
check('aiming at an element resolves it', savePoint?.found === true, savePoint)
await wait(220)

// The overlay is a <hermes-watch> element with a CLOSED shadow root, so page
// script cannot see inside it — which is the point, and also means this test
// has to look the way DevTools looks: CDP's DOM domain, which pierces.
await send('DOM.enable')

/** The agent cursor's on-screen box, read through the closed shadow root.
 *  Returns null when there is no cursor drawn at all. */
async function cursorBox() {
  const { root } = await send('DOM.getDocument', { depth: -1, pierce: true })
  const stack = [root]
  while (stack.length) {
    const node = stack.pop()
    if (node.nodeName?.toLowerCase() === 'svg') {
      try {
        const { model } = await send('DOM.getBoxModel', { nodeId: node.nodeId })
        // content quad: [x1,y1, x2,y2, x3,y3, x4,y4]
        return { height: model.height, width: model.width, x: model.content[0], y: model.content[1] }
      } catch {
        return null
      }
    }
    for (const child of node.children ?? []) stack.push(child)
    for (const shadow of node.shadowRoots ?? []) stack.push(shadow)
    if (node.contentDocument) stack.push(node.contentDocument)
  }
  return null
}

const hostState = await evaluate(`
  (function () {
    var host = document.querySelector('hermes-watch')
    if (!host) return { host: false }
    return {
      host: true,
      // A CLOSED shadow root reads as null from the page. That IS the check:
      // the page cannot reach in to restyle or remove the agent's cursor.
      reachableFromPage: host.shadowRoot !== null,
      inTopLayer: host.hasAttribute('popover')
    }
  })()
`)

const cursorState = await cursorBox()

check('it draws a cursor of its own', !!cursorState && cursorState.width > 0, { cursorState, hostState })
check(
  'the cursor is sealed in a closed shadow root the page cannot reach',
  hostState?.host === true && hostState?.reachableFromPage === false,
  hostState
)
// The fixture's stylesheet sets `svg{display:none!important}`. A cursor in the
// light DOM would have no box at all; having one proves the isolation holds.
check(
  'a hostile page stylesheet cannot hide it',
  !!cursorState && cursorState.width > 0 && cursorState.height > 0,
  cursorState
)

// The point of the whole design: the agent's pointer is a drawing, so the
// person's real mouse is free. Park the real pointer, act, and prove it stayed.
await realMove(12, 12)
const before = await evaluate(`
  (function () {
    window.__realMouse = { x: 0, y: 0 }
    document.addEventListener('mousemove', function (e) {
      window.__realMouse = { x: e.clientX, y: e.clientY }
    })
    return true
  })()
`)
check('the page is watching the real pointer', before === true)

await realMove(12, 12)
const farPoint = await aimAt('#far')
await wait(260)

const afterAim = await evaluate('JSON.stringify(window.__realMouse)')
check(
  'the agent moving does NOT move your mouse',
  JSON.parse(afterAim).x === 12 && JSON.parse(afterAim).y === 12,
  afterAim
)

const movedTo = await cursorBox()
// It travelled down the page toward #far, which sits ~220px below #save.
check(
  'the agent’s cursor travels to what it is acting on',
  !!movedTo && !!cursorState && movedTo.y > cursorState.y + 100,
  { farPoint, from: cursorState?.y, to: movedTo?.y }
)

// ---- 2. the act engine, on a real layout ------------------------------------

console.log('\nreading and acting')

const inventory = await evaluate(`
  JSON.stringify(window.__act(document, window.__holder, { kind: 'elements' }))
`)
const parsed = JSON.parse(inventory)
const refs = (parsed.elements || []).map(e => e.ref)
check('it inventories the page', refs.length > 0, refs.slice(0, 8))
check('refs say what they are', refs.some(r => /save|btn/i.test(r)), refs.slice(0, 8))

const located = await evaluate(`
  JSON.stringify(window.__act(document, window.__holder, {
    kind: 'locate',
    selector: '#save'
  }))
`)
const loc = JSON.parse(located)
check('it can locate a target and measure it', !!loc.point && loc.point.x > 0, loc)

// Trusted input at the located point — the real path, mouse included.
await realClick(loc.point.x, loc.point.y)
const out = await evaluate('document.getElementById("out").textContent')
check('a real click at the located point actually fires the handler', out === 'saved', out)

// The case that motivates trusted input: a menu that only exists while hovered.
// A script-dispatched click on #hidden-item fires at a node that is display:none.
const opener = await evaluate(`
  (function () {
    var r = document.getElementById('menu').getBoundingClientRect()
    return { x: r.x + 40, y: r.y + 12 }
  })()
`)
await realMove(opener.x, opener.y)
await wait(120)
const menuOpen = await evaluate(`
  getComputedStyle(document.querySelector('#menu .items')).display
`)
check('a hover-only menu really opens under the pointer', menuOpen === 'block', menuOpen)

if (menuOpen === 'block') {
  const item = await evaluate(`
    (function () {
      var r = document.getElementById('hidden-item').getBoundingClientRect()
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 }
    })()
  `)
  await send('Input.dispatchMouseEvent', { buttons: 0, type: 'mouseMoved', x: item.x, y: item.y })
  await wait(40)
  await send('Input.dispatchMouseEvent', { button: 'left', buttons: 1, clickCount: 1, type: 'mousePressed', x: item.x, y: item.y })
  await send('Input.dispatchMouseEvent', { button: 'left', buttons: 0, clickCount: 1, type: 'mouseReleased', x: item.x, y: item.y })
  await wait(90)
  const picked = await evaluate('document.getElementById("out").textContent')
  check('and an item inside it can be clicked', picked === 'picked from menu', picked)
}

// ---- 3. the console the agent is now told about -----------------------------

console.log('\nwhat the console reports')

await evaluate(`
  (function () {
    console.warn('missing translation for key "checkout.title"')
    console.error('TypeError: undefined is not a function')
    console.log('just chatter')
    return true
  })()
`)
await wait(150)

check('a console.warn is observable to the embedder', consoleSeen.includes('warning'), consoleSeen)
check('a console.error is observable to the embedder', consoleSeen.includes('error'), consoleSeen)
// The i18n case: warnings are a different level from errors, and the digest
// counts them separately precisely so this one is not swallowed.
check(
  'warnings arrive as their own level, not folded into errors',
  consoleSeen.includes('warning') && consoleSeen.includes('error') &&
    consoleSeen.indexOf('warning') !== consoleSeen.indexOf('error'),
  consoleSeen
)

// ---- done ------------------------------------------------------------------

console.log('')
if (failures) {
  console.log(`${failures} check(s) failed\n`)
  process.exit(1)
}
console.log('all agent browser checks passed\n')
process.exit(0)
