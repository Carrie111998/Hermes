import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  applyWindowsOcclusionCommandLineSwitches,
  windowsOcclusionCommandLineSwitches
} from './windows-occlusion-flags'

test('windows occlusion switches are empty off Windows', () => {
  assert.deepEqual(windowsOcclusionCommandLineSwitches('darwin'), [])
  assert.deepEqual(windowsOcclusionCommandLineSwitches('linux'), [])
})

test('windows occlusion switches opt out of occluded-window backgrounding', () => {
  const flags = windowsOcclusionCommandLineSwitches('win32')

  assert.deepEqual(flags, [
    { switchName: 'disable-backgrounding-occluded-windows' },
    { switchName: 'disable-features', value: 'CalculateNativeWinOcclusion' }
  ])
})

test('applyWindowsOcclusionCommandLineSwitches appends only on win32', () => {
  const calls: Array<[string, string?]> = []
  const appendSwitch = (switchName: string, value?: string) => {
    calls.push(value === undefined ? [switchName] : [switchName, value])
  }

  applyWindowsOcclusionCommandLineSwitches(appendSwitch, 'darwin')
  assert.deepEqual(calls, [])

  applyWindowsOcclusionCommandLineSwitches(appendSwitch, 'win32')
  assert.deepEqual(calls, [
    ['disable-backgrounding-occluded-windows'],
    ['disable-features', 'CalculateNativeWinOcclusion']
  ])
})
