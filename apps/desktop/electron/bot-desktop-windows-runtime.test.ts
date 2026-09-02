import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  BOT_DESKTOP_WINDOWS_VIEWER_PORT_START,
  botDesktopViewerUrl,
  buildBotDesktopLinuxLauncher,
  createBotDesktopWindowsRuntime,
  windowsPathToWslPath
} from './bot-desktop-windows-runtime'

test('maps Windows workspaces into WSL without sharing the host display', () => {
  assert.equal(
    windowsPathToWslPath('C:\\Users\\makim\\.hermes\\profiles\\alpha'),
    '/mnt/c/Users/makim/.hermes/profiles/alpha'
  )
  assert.equal(windowsPathToWslPath('/mnt/c/workspace'), '/mnt/c/workspace')
})

test('allocates a profile viewer URL from the display port', () => {
  const port = BOT_DESKTOP_WINDOWS_VIEWER_PORT_START + 100

  assert.match(botDesktopViewerUrl(port), new RegExp(`127\\.0\\.0\\.1:${port}/vnc\\.html`))
})

test('launcher starts an isolated X11 desktop and bounded noVNC bridge', () => {
  const launcher = buildBotDesktopLinuxLauncher('alpha', ':100', '1280x800x24', 6180, 6000, '/workspace/chrome')

  assert.match(launcher, /Xvfb ':100'/)
  assert.match(launcher, /x11vnc/)
  assert.match(launcher, /websockify/)
  assert.match(launcher, /__HERMES_BOT_DESKTOP_READY__/)
  assert.match(launcher, /for i in 1 2 3/)
})

test('non-Windows hosts fail closed instead of pretending to provide a desktop', async () => {
  const runtime = createBotDesktopWindowsRuntime({ platform: 'linux' })
  const info = await runtime.ensure('alpha')

  assert.equal(info.supported, false)
  assert.equal(info.running, false)
  assert.match(info.error || '', /only when Hermes Desktop runs on Windows/)
})
