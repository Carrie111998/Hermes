import { describe, expect, it, vi } from 'vitest'

vi.mock('@capacitor/app', () => ({ App: { addListener: vi.fn() } }))
vi.mock('@capacitor/browser', () => ({ Browser: { open: vi.fn() } }))
vi.mock('@capacitor/clipboard', () => ({ Clipboard: { write: vi.fn() } }))
vi.mock('@capacitor/core', () => ({ Capacitor: { isNativePlatform: () => true } }))
vi.mock('@capacitor/network', () => ({ Network: { addListener: vi.fn() } }))
vi.mock('@capacitor/local-notifications', () => ({
  LocalNotifications: {
    checkPermissions: vi.fn(),
    createChannel: vi.fn(),
    requestPermissions: vi.fn(),
    schedule: vi.fn(),
  },
}))

import { App } from '@capacitor/app'
import { Browser } from '@capacitor/browser'
import { LocalNotifications } from '@capacitor/local-notifications'
import { Network } from '@capacitor/network'
import { notify, requestMicrophoneAccess, onPowerResume, openExternal } from './native'

describe('requestMicrophoneAccess', () => {
  it('requests audio capture and releases the probe stream', async () => {
    const stop = vi.fn()
    const getUserMedia = vi.fn(async () => ({ getTracks: () => [{ stop }] }))
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia } })

    await expect(requestMicrophoneAccess()).resolves.toBe(true)
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true })
    expect(stop).toHaveBeenCalledOnce()
  })

  it('returns false when microphone permission is denied', async () => {
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => Promise.reject(new DOMException('Denied', 'NotAllowedError'))) }
    })

    await expect(requestMicrophoneAccess()).resolves.toBe(false)
  })
})

describe('openExternal', () => {
  it('opens only credential-free HTTPS links', async () => {
    await openExternal('https://hermes-agent.nousresearch.com/docs')
    await openExternal('intent://malicious')
    await openExternal('https://user:password@example.test')

    expect(Browser.open).toHaveBeenCalledTimes(1)
    expect(Browser.open).toHaveBeenCalledWith({ url: 'https://hermes-agent.nousresearch.com/docs' })
  })
})

describe('notify', () => {
  it('posts an immediate foreground notification without asking for exact-alarm access', async () => {
    vi.mocked(LocalNotifications.checkPermissions).mockResolvedValue({ display: 'granted' })
    vi.mocked(LocalNotifications.createChannel).mockResolvedValue()
    vi.mocked(LocalNotifications.schedule).mockResolvedValue({ notifications: [] })

    await expect(notify({ body: 'Turn complete', kind: 'turnDone', sessionId: 's1', title: 'Hermes' })).resolves.toBe(true)
    expect(LocalNotifications.createChannel).toHaveBeenCalledWith(expect.objectContaining({ id: 'hermes_activity' }))
    expect(LocalNotifications.schedule).toHaveBeenCalledWith({
      notifications: [
        expect.objectContaining({
          channelId: 'hermes_activity',
          foreground: true,
          isExactNotification: false,
          title: 'Hermes',
        }),
      ],
    })
  })
})

describe('onPowerResume', () => {
  it('reconnects after Android resume and removes the native listener on cleanup', async () => {
    const remove = vi.fn()
    const networkRemove = vi.fn()
    const callback = vi.fn()
    let resumeListener: (() => void) | undefined
    vi.mocked(Network.addListener).mockResolvedValue({ remove: networkRemove })
    vi.mocked(App.addListener).mockImplementation(async (eventName, listener) => {
      expect(eventName).toBe('resume')
      resumeListener = listener as () => void
      return { remove }
    })

    const unsubscribe = onPowerResume(callback)
    await vi.waitFor(() => expect(resumeListener).toBeTypeOf('function'))
    resumeListener?.()
    expect(callback).toHaveBeenCalledOnce()

    unsubscribe()
    await vi.waitFor(() => {
      expect(remove).toHaveBeenCalledOnce()
      expect(networkRemove).toHaveBeenCalledOnce()
    })
  })

  it('reconnects when Android regains network connectivity', async () => {
    const callback = vi.fn()
    let networkListener: ((status: { connected: boolean }) => void) | undefined
    vi.mocked(Network.addListener).mockImplementation(async (eventName, listener) => {
      expect(eventName).toBe('networkStatusChange')
      networkListener = listener as (status: { connected: boolean }) => void
      return { remove: vi.fn() }
    })

    onPowerResume(callback)
    await vi.waitFor(() => expect(networkListener).toBeTypeOf('function'))
    networkListener?.({ connected: false })
    networkListener?.({ connected: true })

    expect(callback).toHaveBeenCalledOnce()
  })
})
