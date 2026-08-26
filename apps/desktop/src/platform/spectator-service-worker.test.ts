import { beforeEach, describe, expect, it, vi } from 'vitest'

import { registerSpectatorServiceWorker, spectatorServiceWorkerUrl } from './spectator-service-worker'

describe('spectator service worker registration', () => {
  beforeEach(() => {
    window.__HERMES_BASE_PATH__ = ''
    window.__HERMES_SPECTATOR_BASE_PATH__ = ''
    window.__HERMES_SPECTATOR__ = false
  })

  it('does nothing outside explicit spectator mode', async () => {
    await expect(registerSpectatorServiceWorker()).resolves.toBeNull()
  })

  it('honors a reverse-proxy base path', () => {
    window.__HERMES_SPECTATOR_BASE_PATH__ = '/hermes/spectator/'
    expect(spectatorServiceWorkerUrl()).toBe('/hermes/spectator/spectator-sw.js')
  })

  it('registers without forcing a waiting worker to activate', async () => {
    window.__HERMES_SPECTATOR__ = true
    const addEventListener = vi.fn()
    const registration = {
      addEventListener,
      installing: null,
      waiting: { postMessage: vi.fn() }
    } as unknown as ServiceWorkerRegistration
    const register = vi.fn().mockResolvedValue(registration)
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: { controller: {}, register }
    })
    const dispatch = vi.spyOn(window, 'dispatchEvent')

    await expect(registerSpectatorServiceWorker()).resolves.toBe(registration)

    expect(register).toHaveBeenCalledWith('/spectator-sw.js', { scope: '/' })
    expect(registration.waiting?.postMessage).not.toHaveBeenCalled()
    expect(dispatch.mock.calls.some(([event]) => event.type === 'hermes:spectator-update-ready')).toBe(true)
  })
})
