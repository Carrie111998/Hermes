#!/usr/bin/env node

import { existsSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import process from 'node:process'

import { chromium } from 'playwright'

const url = process.env.HERMES_BROWSER_HOST_URL || process.argv[2] || 'http://127.0.0.1:9119/'
const allowGatewayFailure = process.env.HERMES_BROWSER_ALLOW_GATEWAY_FAILURE === '1'
const requireTerminal = process.env.HERMES_BROWSER_REQUIRE_TERMINAL === '1'

function findExecutable() {
  const explicit = process.env.HERMES_BROWSER_EXECUTABLE?.trim()
  if (explicit) return explicit

  const candidates = process.platform === 'win32'
    ? [
        'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
        'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
        'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'
      ]
    : ['chromium-browser', 'chromium', 'google-chrome', 'google-chrome-stable']

  for (const candidate of candidates) {
    if (candidate.includes('\\') && existsSync(candidate)) return candidate
    if (!candidate.includes('\\')) {
      const found = spawnSync('sh', ['-lc', `command -v ${candidate}`], { encoding: 'utf8' })
      const path = found.status === 0 ? found.stdout.trim() : ''
      if (path) return path
    }
  }

  throw new Error('No Chromium-compatible browser found; set HERMES_BROWSER_EXECUTABLE')
}

const ignoredConsoleError = text =>
  text.includes("Blocked call to navigator.vibrate because user hasn't tapped") ||
  (allowGatewayFailure && text.includes('WebSocket connection to'))

const browser = await chromium.launch({
  executablePath: findExecutable(),
  headless: true,
  args: ['--disable-dev-shm-usage']
})

try {
  for (const viewport of [
    { width: 390, height: 844 },
    { width: 320, height: 568 }
  ]) {
    const page = await browser.newPage({ viewport })
    const consoleErrors = []
    const pageErrors = []
    const failedRequests = []

    page.on('console', message => {
      if (message.type() === 'error' && !ignoredConsoleError(message.text())) {
        consoleErrors.push(message.text())
      }
    })
    page.on('pageerror', error => pageErrors.push(error.message))
    page.on('requestfailed', request => {
      const failure = request.failure()?.errorText || 'request failed'
      if (!(allowGatewayFailure && request.url().includes('/api/ws'))) {
        failedRequests.push(`${request.url()} (${failure})`)
      }
    })

    const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30_000 })
    await page.waitForTimeout(4_000)

    const state = await page.evaluate(() => {
      const requiredBridgeMethods = [
        'api',
        'getConnection',
        'getRecentLogs',
        'normalizePreviewTarget',
        'readDir',
        'readFileText',
        'revealLogs',
        'saveClipboardImage',
        'watchPreviewFile'
      ]
      const text = document.body.innerText || ''
      return {
        bodyScrollWidth: document.body.scrollWidth,
        bridge: Boolean(window.hermesDesktop),
        clientWidth: document.documentElement.clientWidth,
        desktopBootFailed: text.includes('Desktop boot failed'),
        host: document.documentElement.dataset.hermesDesktopHost || '',
        requiredBridgeMethods: requiredBridgeMethods.every(
          key => typeof window.hermesDesktop?.[key] === 'function'
        ),
        rootError: text.includes('Something broke in the interface'),
        scrollWidth: document.documentElement.scrollWidth,
        sessionToken: Boolean(window.__HERMES_SESSION_TOKEN__),
        title: document.title
      }
    })

    let terminalState = { skipped: true }
    if (requireTerminal && viewport.width === 390) {
      terminalState = await page.evaluate(async () => {
        const terminal = window.hermesDesktop?.terminal
        if (!terminal) {
          return { error: 'terminal bridge missing', ok: false, skipped: false }
        }

        let session
        try {
          session = await terminal.start({ cols: 48, rows: 16 })
          let output = ''
          const stop = terminal.onData(session.id, chunk => {
            output += chunk
          })
          const marker = `HERMES_TERMUX_TERMINAL_${Math.random().toString(36).slice(2, 10)}`
          await terminal.write(session.id, `printf '${marker}\\n'\r`)
          const deadline = Date.now() + 8_000
          while (!output.includes(marker) && Date.now() < deadline) {
            await new Promise(resolve => setTimeout(resolve, 50))
          }
          const cwd = await terminal.cwd(session.id)
          stop()
          await terminal.dispose(session.id)

          return {
            cwd,
            ok: output.includes(marker),
            outputTail: output.slice(-800),
            shell: session.shell,
            skipped: false
          }
        } catch (error) {
          if (session?.id) {
            await terminal.dispose(session.id).catch(() => undefined)
          }
          return {
            error: error instanceof Error ? error.message : String(error),
            ok: false,
            skipped: false
          }
        }
      })
    }

    const failures = []
    if (!response || response.status() >= 400) failures.push(`HTTP ${response?.status() ?? 'no response'}`)
    if (state.host !== 'browser' || !state.bridge) failures.push('browser Desktop bridge did not install')
    if (!state.sessionToken) failures.push('loopback session token was not injected')
    if (!state.requiredBridgeMethods) failures.push('required browser Desktop bridge methods are missing')
    if (state.rootError) failures.push('renderer reached its root error boundary')
    if (!allowGatewayFailure && state.desktopBootFailed) failures.push('Desktop could not connect to the Hermes gateway')
    if (requireTerminal && viewport.width === 390 && !terminalState.ok) {
      failures.push(`browser Desktop shell terminal failed: ${terminalState.error || terminalState.outputTail || 'marker missing'}`)
    }
    if (state.scrollWidth > state.clientWidth || state.bodyScrollWidth > state.clientWidth) {
      failures.push(
        `horizontal overflow: viewport=${state.clientWidth}, html=${state.scrollWidth}, body=${state.bodyScrollWidth}`
      )
    }
    failures.push(...pageErrors.map(error => `pageerror: ${error}`))
    failures.push(...consoleErrors.map(error => `console: ${error}`))
    failures.push(...failedRequests.map(error => `request: ${error}`))

    console.log(
      JSON.stringify(
        {
          failures,
          http: response?.status() ?? null,
          state,
          terminalState,
          viewport
        },
        null,
        2
      )
    )

    await page.close()

    if (failures.length) {
      process.exitCode = 1
      break
    }
  }
} finally {
  await browser.close()
}
