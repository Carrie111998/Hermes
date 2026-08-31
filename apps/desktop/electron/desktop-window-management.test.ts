import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createDesktopWindowManagement } from './desktop-window-management'

function makeDeps() {
  const registryCalls = []
  const wakeIndicatorController = { close: () => {} }

  return {
    registryCalls,
    wakeIndicatorController,
    deps: {
      createSessionWindowRegistry: () => ({
        openOrFocus: (id, factory) => {
          registryCalls.push({ id, factory })

          return `opened:${id}`
        }
      }),
      createWakeIndicatorWindowController: options => {
        assert.equal(typeof options.wireWindow, 'function')

        return wakeIndicatorController
      }
    }
  }
}

test('window-management factory preserves the main-process seam identities', () => {
  const { deps, registryCalls, wakeIndicatorController } = makeDeps()
  const management = createDesktopWindowManagement(deps)

  assert.equal(management.wakeIndicatorController, wakeIndicatorController)
  assert.equal(management.wireCommonWindowHandlers.name, 'wireCommonWindowHandlers')
  assert.equal(management.wireWindowReveal.name, 'wireWindowReveal')
  assert.equal(management.createSessionWindow(' session-1 '), 'opened: session-1 ')
  assert.equal(registryCalls.length, 1)
  assert.equal(registryCalls[0].id, ' session-1 ')
  assert.equal(typeof registryCalls[0].factory, 'function')
})
