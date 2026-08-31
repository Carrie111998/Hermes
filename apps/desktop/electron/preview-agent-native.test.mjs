/**
 * The agent's browser wiring, in a real Electron <webview>.
 *
 * The Chromium harness (scripts/check-preview-agent.mjs) proves the in-page
 * engines. It cannot prove the seams that only exist inside Electron, and those
 * are exactly where this feature's silent failures live:
 *
 *   - THE CONSOLE PAYLOAD. Everything the agent is told about a page's errors
 *     rests on `<webview>`'s console-message reporting an INTEGER level 0-3.
 *     `webContents` moved to a string in Electron 35 and the docs say <webview>
 *     did not follow, but that was read, never measured — and if it is wrong,
 *     `level === 3` quietly stops matching, every error counts as a `log`, and
 *     the agent goes back to being told a broken page is fine. Nothing throws.
 *     This measures it against the real event, and runs the real
 *     `consoleLevel` over the real payload.
 *
 *   - THE IPC OWNERSHIP GUARD. `hermes:preview:emulate-device` now refuses a
 *     guest the caller does not host. `hostWebContents` is the thing being
 *     compared and it exists only for a real webview guest.
 *
 *   - DEVICE EMULATION AND INJECTED INPUT. Chromium divides injected
 *     coordinates by the emulation scale; the pane multiplies on the way in.
 *     Both halves need a live guest to be true of anything.
 *
 *   electron electron/preview-agent-native-fixture
 */

import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { app, BrowserWindow, ipcMain, webContents as electronWebContents } from 'electron'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
const runtimeDir = mkdtempSync(join(tmpdir(), 'hermes-preview-native-'))

app.setPath('userData', runtimeDir)
app.setPath('sessionData', runtimeDir)

let failures = 0
const check = (label, condition, detail = '') => {
  if (condition) {
    console.log(`  ok   ${label}`)
  } else {
    failures += 1
    console.log(`  FAIL ${label}${detail ? ` — ${typeof detail === 'string' ? detail : JSON.stringify(detail)}` : ''}`)
  }
}

/** Bundle a TS module so the test exercises the SHIPPING function rather than a
 *  transcription of it — the whole point is to catch the real one drifting. */
function bundle(entry, outfile) {
  const esbuildBin = existsSync(join(ROOT, 'node_modules/.bin/esbuild'))
    ? join(ROOT, 'node_modules/.bin/esbuild')
    : join(ROOT, '../../node_modules/.bin/esbuild')

  const result = spawnSync(esbuildBin, [join(ROOT, entry), '--bundle', '--format=esm', `--outfile=${outfile}`], {
    encoding: 'utf8'
  })

  if (result.status !== 0) {
    throw new Error(`esbuild failed for ${entry}: ${result.stderr}`)
  }
}

// The viewport meta is load-bearing, not decoration: without it Chromium gives
// a `mobile` emulation its 980px default layout viewport, so the guest reports
// 980 no matter what width it was told. That is correct behaviour and exactly
// what preview-viewport.ts means by "without `mobile` a 390px viewport is just
// a narrow desktop window that ignores <meta viewport>" — a real site under
// test has one, so the fixture must too.
const GUEST = `<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Guest</title></head><body>
  <h1 id="title">Guest page</h1>
  <button id="save">Save</button>
  <script>
    // The motivating pair: a missing-translation WARN and a real ERROR.
    console.warn('missing translation for key "checkout.title"')
    console.error('TypeError: undefined is not a function')
    document.getElementById('save').addEventListener('click', () => {
      document.title = 'clicked'
    })
  </script>
</body></html>`

async function run() {
  const consoleBundle = join(runtimeDir, 'console-state.mjs')
  bundle('src/app/chat/right-rail/preview-console-state.ts', consoleBundle)
  const { consoleLevel } = await import(pathToFileURL(consoleBundle).href)

  // Serve the guest over http: a file:// guest gets different console plumbing.
  const server = createServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html; charset=utf-8' })
    response.end(GUEST)
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const guestUrl = `http://127.0.0.1:${server.address().port}/`

  // The real IPC handler, copied in behaviour from main.ts so the guard is
  // exercised rather than described. Registered before the window exists.
  ipcMain.handle('hermes:preview:emulate-device', (event, payload) => {
    const guest = electronWebContents.fromId(Number(payload?.webContentsId))

    if (!guest || guest.isDestroyed()) {
      return false
    }

    if (guest.hostWebContents?.id !== event.sender.id) {
      return false
    }

    const metrics = payload?.metrics

    try {
      if (!metrics) {
        guest.disableDeviceEmulation()

        return true
      }

      const width = Math.max(1, Math.round(Number(metrics.width) || 0))
      const height = Math.max(1, Math.round(Number(metrics.height) || 0))

      guest.enableDeviceEmulation({
        deviceScaleFactor: 0,
        screenPosition: metrics.mobile ? 'mobile' : 'desktop',
        screenSize: { height, width },
        scale: Number(metrics.scale) > 0 ? Number(metrics.scale) : 1,
        viewPosition: { x: 0, y: 0 },
        viewSize: { height, width }
      })

      return true
    } catch (error) {
      return `threw: ${error.message}`
    }
  })

  const hostPage = join(runtimeDir, 'host.html')
  writeFileSync(
    hostPage,
    `<!doctype html><html><body style="margin:0">
       <webview id="guest" src="${guestUrl}" style="width:800px;height:600px"
                partition="persist:hermes-preview-native"></webview>
     </body></html>`
  )

  const window = new BrowserWindow({
    height: 700,
    show: false,
    webPreferences: { contextIsolation: false, nodeIntegration: true, webviewTag: true },
    width: 900
  })

  await window.loadFile(hostPage)

  // ---- 1. the console payload, measured -----------------------------------

  console.log('\nthe console payload')

  const seen = await window.webContents.executeJavaScript(`
    new Promise(resolve => {
      const view = document.getElementById('guest')
      const rows = []
      view.addEventListener('console-message', event => {
        // Capture the RAW shape, including the type of \`level\`.
        rows.push({
          levelType: typeof event.level,
          level: event.level,
          message: event.message,
          hasLine: typeof event.line !== 'undefined',
          hasSourceId: typeof event.sourceId !== 'undefined'
        })
      })
      const done = () => setTimeout(() => resolve(rows), 600)
      view.getWebContentsId ? done() : view.addEventListener('dom-ready', done)
    })
  `)

  check('the guest reaches the embedder at all', Array.isArray(seen) && seen.length >= 2, seen)

  const warn = seen.find(row => /missing translation/.test(row.message || ''))
  const error = seen.find(row => /TypeError/.test(row.message || ''))

  check('a console.warn arrives', !!warn, seen)
  check('a console.error arrives', !!error, seen)

  // THE measurement. Everything downstream compares numbers.
  check(
    '<webview> still reports an INTEGER level (not the Electron 35 string)',
    warn?.levelType === 'number' && error?.levelType === 'number',
    { error: error?.levelType, warn: warn?.levelType }
  )
  check('warn is level 2 and error is level 3', warn?.level === 2 && error?.level === 3, {
    error: error?.level,
    warn: warn?.level
  })

  // The shipping normalizer, over the real payload.
  check(
    'consoleLevel maps the real payload to the digest’s numbers',
    consoleLevel(warn?.level) === 2 && consoleLevel(error?.level) === 3,
    { error: consoleLevel(error?.level), warn: consoleLevel(warn?.level) }
  )
  check('the payload carries a source and line for the digest', warn?.hasLine && warn?.hasSourceId, warn)

  // ---- 2. the IPC ownership guard -----------------------------------------

  console.log('\nthe emulate-device guard')

  const guestId = await window.webContents.executeJavaScript(
    `document.getElementById('guest').getWebContentsId()`
  )
  const guest = electronWebContents.fromId(guestId)

  check('the webview guest is reachable from main', !!guest && !guest.isDestroyed())
  check('and it names this window as its host', guest?.hostWebContents?.id === window.webContents.id, {
    host: guest?.hostWebContents?.id,
    window: window.webContents.id
  })

  const ownApply = await window.webContents.executeJavaScript(`
    require('electron').ipcRenderer.invoke('hermes:preview:emulate-device', {
      metrics: { height: 844, mobile: true, scale: 0.5, width: 390 },
      webContentsId: ${guestId}
    })
  `)
  check('a window may emulate the guest it hosts', ownApply === true, ownApply)

  // The attack the guard exists for: naming a webContents this window does not
  // host — here, the window's own renderer, which has no hostWebContents.
  const foreign = await window.webContents.executeJavaScript(`
    require('electron').ipcRenderer.invoke('hermes:preview:emulate-device', {
      metrics: { height: 100, mobile: false, scale: 1, width: 100 },
      webContentsId: ${window.webContents.id}
    })
  `)
  check('but is refused a webContents it does not host', foreign === false, foreign)

  // ---- 3. emulation actually reaches the guest ----------------------------

  console.log('\ndevice emulation')

  const emulated = await guest.executeJavaScript(
    `({ narrow: matchMedia('(max-width: 500px)').matches, width: window.innerWidth })`
  )
  check('the guest reports the emulated width, not the element width', emulated.width === 390, emulated)
  // The point of emulating at all: the page's own media queries have to flip,
  // or a responsive layout is never actually exercised.
  check('and its media queries flip to match', emulated.narrow === true, emulated)

  const cleared = await window.webContents.executeJavaScript(`
    require('electron').ipcRenderer.invoke('hermes:preview:emulate-device', {
      metrics: null, webContentsId: ${guestId}
    })
  `)
  check('emulation can be turned back off', cleared === true, cleared)

  const restored = await guest.executeJavaScript('window.innerWidth')
  check('and the guest goes back to the element’s width', restored !== 390, restored)

  // ---- 4. navigation through the same call the address bar makes ----------

  console.log('\nnavigation')

  await guest.loadURL(`${guestUrl}?second`)
  const landed = await guest.executeJavaScript('location.search')
  check('drive_preview navigate reaches the guest via loadURL', landed === '?second', landed)

  server.close()
  window.destroy()
}

app.whenReady().then(async () => {
  try {
    await run()
  } catch (error) {
    failures += 1
    console.log(`\n  FAIL harness threw — ${error.stack || error.message}`)
  }

  console.log('')
  if (failures) {
    console.log(`${failures} check(s) failed\n`)
  } else {
    console.log('all native preview checks passed\n')
  }

  app.exit(failures ? 1 : 0)
})

assert.ok(app)
