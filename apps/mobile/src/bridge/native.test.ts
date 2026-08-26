import { beforeEach, describe, expect, it, vi } from 'vitest'

const { mobileCapabilities } = vi.hoisted(() => ({
  mobileCapabilities: { requestBackgroundReliability: vi.fn(), requestMedia: vi.fn() },
}))

vi.mock('@capacitor/app', () => ({ App: { addListener: vi.fn() } }))
vi.mock('@capacitor/browser', () => ({ Browser: { open: vi.fn() } }))
vi.mock('@capacitor/clipboard', () => ({ Clipboard: { write: vi.fn() } }))
vi.mock('@capacitor/core', () => ({
  Capacitor: { isNativePlatform: () => true },
  registerPlugin: () => mobileCapabilities,
}))
vi.mock('@capacitor/camera', () => ({
  Camera: {
    checkPermissions: vi.fn(),
    requestPermissions: vi.fn(),
    takePhoto: vi.fn(),
  },
  MediaType: { Photo: 0 },
}))
vi.mock('@capacitor/network', () => ({
  Network: { addListener: vi.fn() },
}))
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
import { Camera, MediaType } from '@capacitor/camera'
import { LocalNotifications } from '@capacitor/local-notifications'
import { Network } from '@capacitor/network'
import {
  captureCameraPhoto,
  notify,
  onPowerResume,
  openExternal,
  requestInitialMobilePermissions,
  requestMicrophoneAccess,
  requestNotificationPermission,
} from './native'

beforeEach(() => {
  vi.mocked(Camera.checkPermissions).mockReset()
  vi.mocked(Camera.requestPermissions).mockReset()
  vi.mocked(Camera.takePhoto).mockReset()
  vi.mocked(LocalNotifications.checkPermissions).mockReset()
  vi.mocked(LocalNotifications.createChannel).mockReset()
  vi.mocked(LocalNotifications.requestPermissions).mockReset()
  vi.mocked(LocalNotifications.schedule).mockReset()
  mobileCapabilities.requestBackgroundReliability.mockReset()
  mobileCapabilities.requestMedia.mockReset()
  vi.unstubAllGlobals()
})

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

describe('captureCameraPhoto', () => {
  it('requests camera permission only after capture is invoked and returns the captured image bytes', async () => {
    vi.mocked(Camera.checkPermissions).mockResolvedValue({ camera: 'prompt', photos: 'granted' })
    vi.mocked(Camera.requestPermissions).mockResolvedValue({ camera: 'granted', photos: 'granted' })
    vi.mocked(Camera.takePhoto).mockResolvedValue({ saved: false, type: MediaType.Photo, webPath: 'https://camera.example/photo.jpg' })
    const fetchMock = vi.fn(async () => new Response(new Blob(['camera bytes'], { type: 'image/jpeg' })))
    vi.stubGlobal('fetch', fetchMock)

    const photo = await captureCameraPhoto()

    expect(Camera.requestPermissions).toHaveBeenCalledWith({ permissions: ['camera'] })
    expect(Camera.takePhoto).toHaveBeenCalledWith(expect.objectContaining({ correctOrientation: true, quality: 85, saveToGallery: false }))
    await expect(photo?.text()).resolves.toBe('camera bytes')
  })

  it('does not open the camera when the user declines permission', async () => {
    vi.mocked(Camera.checkPermissions).mockResolvedValue({ camera: 'prompt', photos: 'granted' })
    vi.mocked(Camera.requestPermissions).mockResolvedValue({ camera: 'denied', photos: 'granted' })

    await expect(captureCameraPhoto()).resolves.toBeNull()
    expect(Camera.takePhoto).not.toHaveBeenCalled()
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

describe('requestNotificationPermission', () => {
  it('opens Android notification permission only from an explicit user action', async () => {
    vi.mocked(LocalNotifications.checkPermissions).mockResolvedValue({ display: 'prompt' })
    vi.mocked(LocalNotifications.requestPermissions).mockResolvedValue({ display: 'granted' })
    vi.mocked(LocalNotifications.createChannel).mockResolvedValue()

    await expect(requestNotificationPermission()).resolves.toBe(true)
    expect(LocalNotifications.requestPermissions).toHaveBeenCalledOnce()
  })
})

describe('requestInitialMobilePermissions', () => {
  it('requests the user-approved notification, camera/gallery, microphone, and background-reliability capabilities', async () => {
    vi.mocked(LocalNotifications.checkPermissions).mockResolvedValue({ display: 'prompt' })
    vi.mocked(LocalNotifications.requestPermissions).mockResolvedValue({ display: 'granted' })
    vi.mocked(LocalNotifications.createChannel).mockResolvedValue()
    vi.mocked(Camera.checkPermissions).mockResolvedValue({ camera: 'prompt', photos: 'granted' })
    vi.mocked(Camera.requestPermissions).mockResolvedValue({ camera: 'granted', photos: 'granted' })
    mobileCapabilities.requestMedia.mockResolvedValue({ granted: true, supported: true })
    mobileCapabilities.requestBackgroundReliability.mockResolvedValue({ exempt: false, requested: true, supported: true })
    const stop = vi.fn()
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop }] })) },
    })

    await expect(requestInitialMobilePermissions()).resolves.toEqual({
      backgroundReliabilityRequested: true,
      camera: true,
      microphone: true,
      notifications: true,
      photos: true,
    })
    expect(LocalNotifications.requestPermissions).toHaveBeenCalledOnce()
    expect(Camera.requestPermissions).toHaveBeenCalledWith({ permissions: ['camera'] })
    expect(stop).toHaveBeenCalledOnce()
    expect(mobileCapabilities.requestMedia).toHaveBeenCalledOnce()
    expect(mobileCapabilities.requestBackgroundReliability).toHaveBeenCalledOnce()
  })
})

describe('notify', () => {
  it('does not surprise-prompt while dispatching an automatic notification', async () => {
    vi.mocked(LocalNotifications.checkPermissions).mockResolvedValue({ display: 'denied' })

    await expect(notify({ body: 'Turn complete', kind: 'turnDone', sessionId: 's1', title: 'Hermes' })).resolves.toBe(false)
    expect(LocalNotifications.requestPermissions).not.toHaveBeenCalled()
    expect(LocalNotifications.schedule).not.toHaveBeenCalled()
  })

  it('posts an immediate foreground notification without asking for exact-alarm access', async () => {
    vi.mocked(LocalNotifications.checkPermissions).mockResolvedValue({ display: 'granted' })
    vi.mocked(LocalNotifications.createChannel).mockResolvedValue()
    vi.mocked(LocalNotifications.schedule).mockResolvedValue({ notifications: [] })

    await expect(notify({ body: 'Turn complete', kind: 'turnDone', sessionId: 's1', title: 'Hermes' })).resolves.toBe(true)
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

  it('routes action-needed alerts to Android’s higher-priority attention channel', async () => {
    vi.mocked(LocalNotifications.checkPermissions).mockResolvedValue({ display: 'granted' })
    vi.mocked(LocalNotifications.schedule).mockResolvedValue({ notifications: [] })

    await expect(notify({ body: 'Approve this command', kind: 'approval', sessionId: 's2', title: 'Hermes' })).resolves.toBe(true)
    expect(LocalNotifications.schedule).toHaveBeenCalledWith({
      notifications: [expect.objectContaining({ channelId: 'hermes_attention' })],
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
