import assert from 'node:assert/strict'

import type { WebContents } from 'electron'
import { test } from 'vitest'

import {
  enforcePreviewWebviewPolicy,
  installPreviewWebviewNavigationGuard,
  isAllowedPreviewWebviewUrl,
  PREVIEW_WEBVIEW_PARTITION
} from './webview-guard'

function fixture({
  isAllowedUrl = isAllowedPreviewWebviewUrl,
  partition = PREVIEW_WEBVIEW_PARTITION,
  src = 'http://127.0.0.1:5173/'
} = {}) {
  let prevented = false

  const webPreferences = {
    allowRunningInsecureContent: true,
    contextIsolation: false,
    disableBlinkFeatures: 'DisableAttackerFeature',
    enableBlinkFeatures: 'EnableAttackerFeature',
    experimentalFeatures: true,
    nodeIntegration: true,
    nodeIntegrationInSubFrames: true,
    nodeIntegrationInWorker: true,
    partition: 'persist:attacker',
    plugins: true,
    preload: '/tmp/attacker-preload.js',
    sandbox: false,
    webSecurity: false,
    webviewTag: true
  }

  const params: Record<string, string> = {
    allowpopups: 'true',
    blinkfeatures: 'EnableAttackerFeature',
    disableblinkfeatures: 'DisableAttackerFeature',
    nodeintegration: 'true',
    nodeintegrationinsubframes: 'true',
    partition,
    preload: '/tmp/attacker-preload.js',
    src,
    webpreferences: 'nodeIntegration=yes,preload=/tmp/attacker-preload.js'
  }

  enforcePreviewWebviewPolicy({ preventDefault: () => (prevented = true) }, webPreferences, params, isAllowedUrl)

  return { params, prevented, webPreferences }
}

test('pre-attach policy strips renderer preload and unsafe preferences', () => {
  const { params, prevented, webPreferences } = fixture()

  assert.equal(prevented, false)
  assert.equal(webPreferences.preload, undefined)
  assert.equal(webPreferences.enableBlinkFeatures, undefined)
  assert.equal(webPreferences.disableBlinkFeatures, undefined)
  assert.equal(webPreferences.nodeIntegration, false)
  assert.equal(webPreferences.nodeIntegrationInSubFrames, false)
  assert.equal(webPreferences.nodeIntegrationInWorker, false)
  assert.equal(webPreferences.contextIsolation, true)
  assert.equal(webPreferences.sandbox, true)
  assert.equal(webPreferences.webSecurity, true)
  assert.equal(webPreferences.webviewTag, false)
  assert.equal(webPreferences.partition, PREVIEW_WEBVIEW_PARTITION)
  assert.equal(params.preload, undefined)
  assert.equal(params.blinkfeatures, undefined)
  assert.equal(params.disableblinkfeatures, undefined)
  assert.equal(params.webpreferences, undefined)
  assert.equal(params.allowpopups, undefined)
  assert.equal(params.partition, PREVIEW_WEBVIEW_PARTITION)
})

test('pre-attach policy rejects remote and non-preview guest sources', () => {
  for (const source of ['https://attacker.example/', 'data:text/html,owned', 'about:blank']) {
    const { prevented } = fixture({ src: source })

    assert.equal(prevented, true, source)
  }

  assert.equal(fixture({ partition: 'persist:attacker' }).prevented, true)
  assert.equal(isAllowedPreviewWebviewUrl('file:///tmp/preview.html'), true)
  assert.equal(isAllowedPreviewWebviewUrl('file://server/share/report.html'), false)
  assert.equal(isAllowedPreviewWebviewUrl('file://server/share/report.html', { allowWindowsUnc: true }), true)
  assert.equal(isAllowedPreviewWebviewUrl('file://server'), false)
  assert.equal(
    fixture({
      isAllowedUrl: url => isAllowedPreviewWebviewUrl(url, { allowWindowsUnc: true }),
      src: 'file://server/share/report.html'
    }).prevented,
    false
  )
  assert.equal(isAllowedPreviewWebviewUrl('https://localhost:3000/'), true)
  assert.equal(isAllowedPreviewWebviewUrl('https://[::1]:3000/'), true)
  assert.equal(isAllowedPreviewWebviewUrl('https://[::2]:3000/'), false)
  assert.equal(isAllowedPreviewWebviewUrl('https://attacker.example/'), false)
})

test('guest navigation guard blocks main-frame escapes but leaves safe subframes alone', () => {
  const listeners = new Map<
    string,
    (event: { isMainFrame?: boolean; preventDefault: () => void; url: string }) => void
  >()

  installPreviewWebviewNavigationGuard({
    on(event, listener) {
      listeners.set(
        event,
        listener as (event: { isMainFrame?: boolean; preventDefault: () => void; url: string }) => void
      )

      return this
    }
  } as Pick<WebContents, 'on'>)

  for (const event of ['will-navigate', 'will-frame-navigate', 'will-redirect']) {
    const listener = listeners.get(event)

    assert.ok(listener, `${event} listener was not installed`)

    let prevented = false
    listener({
      isMainFrame: true,
      preventDefault: () => (prevented = true),
      url: 'https://attacker.example/redirect'
    })
    assert.equal(prevented, true, `${event} must block remote navigation`)

    prevented = false
    listener({
      isMainFrame: true,
      preventDefault: () => (prevented = true),
      url: event === 'will-frame-navigate' ? 'file:///tmp/preview.html' : 'http://localhost:5173/preview'
    })
    assert.equal(prevented, false, `${event} must preserve allowed preview navigation`)

    prevented = false
    listener({
      isMainFrame: false,
      preventDefault: () => (prevented = true),
      url: 'https://attacker.example/embedded'
    })
    assert.equal(prevented, false, `${event} must preserve non-main-frame embeds`)

    prevented = false
    listener({
      isMainFrame: false,
      preventDefault: () => (prevented = true),
      url: event === 'will-redirect' ? 'about:srcdoc' : 'about:blank'
    })
    assert.equal(prevented, false, `${event} must preserve safe about subframes`)
  }
})
