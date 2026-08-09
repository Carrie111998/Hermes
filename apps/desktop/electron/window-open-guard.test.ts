import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { installWindowOpenDenyGuard } from './window-open-guard'

test('window-open guard denies every guest popup request', () => {
  let handler: ((details: unknown) => { action: 'deny' }) | undefined

  installWindowOpenDenyGuard({
    setWindowOpenHandler(next) {
      handler = next as (details: unknown) => { action: 'deny' }
    }
  })

  assert.ok(handler)
  assert.deepEqual(handler({ disposition: 'foreground-tab', url: 'https://attacker.example/popup' }), {
    action: 'deny'
  })
  assert.deepEqual(handler({ disposition: 'new-window', url: 'file:///sensitive' }), { action: 'deny' })
})

test('a denied popup request does not reach an external opener', () => {
  let handler: ((details: unknown) => { action: 'deny' }) | undefined
  const openExternal = vi.fn()

  installWindowOpenDenyGuard({
    setWindowOpenHandler(next) {
      handler = details => {
        const response = (next as unknown as (details: unknown) => { action: 'deny' | 'allow' })(details)

        if (response.action !== 'deny') {
          openExternal((details as { url: string }).url)
        }

        return response as { action: 'deny' }
      }
    }
  })

  const response = handler?.({ disposition: 'new-window', url: 'https://attacker.example/' })

  assert.deepEqual(response, { action: 'deny' })
  assert.equal(openExternal.mock.calls.length, 0)
})
