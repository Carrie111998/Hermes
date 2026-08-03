import assert from 'node:assert/strict'

import { test } from 'vitest'

import { MANAGED_ABOUT_COPYRIGHT, nativeAboutPanelOptions, nativeAppIconCandidates } from './native-branding'

test('managed About refresh preserves the client version and approved legal attribution', () => {
  const options = nativeAboutPanelOptions({
    applicationName: 'evaOS Agent',
    appVersion: '2026.7.20-es.9',
    managed: true,
    upstreamVersion: '0.18.2'
  })

  assert.deepEqual(options, {
    applicationName: 'evaOS Agent',
    applicationVersion: '2026.7.20-es.9',
    copyright: MANAGED_ABOUT_COPYRIGHT
  })
  assert.equal(
    options.copyright,
    'Copyright © 2026 Electric Sheep. Built on Hermes Agent by Nous Research under the MIT License.'
  )
})

test('upstream About refresh still follows the live backend version', () => {
  assert.deepEqual(
    nativeAboutPanelOptions({
      applicationName: 'Hermes',
      appVersion: '0.0.2',
      managed: false,
      upstreamVersion: '0.18.2'
    }),
    {
      applicationName: 'Hermes',
      applicationVersion: '0.18.2',
      copyright: 'Copyright © 2026 Nous Research'
    }
  )
})

test('native windows select only Eva brand assets', () => {
  assert.deepEqual(
    nativeAppIconCandidates({
      appRoot: '/app',
      isWindows: true,
      resourcesPath: '/resources',
      unpackedAppRoot: '/app-unpacked'
    }),
    [
      '/resources/eva.ico',
      '/app/assets/eva.ico',
      '/app/public/eva.png',
      '/app/dist/eva.png',
      '/app-unpacked/dist/eva.png'
    ]
  )
})
