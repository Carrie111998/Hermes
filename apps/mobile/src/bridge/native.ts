/**
 * native.ts — the handful of window.hermesDesktop methods that map cleanly onto
 * Capacitor plugins. Everything else is stubbed in stubs.ts.
 */

import { App } from '@capacitor/app'
import { Browser } from '@capacitor/browser'
import { Clipboard } from '@capacitor/clipboard'
import { Camera } from '@capacitor/camera'
import { Capacitor, registerPlugin } from '@capacitor/core'
import { LocalNotifications } from '@capacitor/local-notifications'
import { Network } from '@capacitor/network'

import type { HermesNotification } from '@/global'

interface MobileCapabilitiesPlugin {
  requestBackgroundReliability(): Promise<{ exempt: boolean; requested: boolean; supported: boolean }>
  requestMedia(): Promise<{ granted: boolean; supported: boolean }>
}

const MobileCapabilities = registerPlugin<MobileCapabilitiesPlugin>('MobileCapabilities')

export async function writeClipboard(text: string): Promise<boolean> {
  try {
    await Clipboard.write({ string: text })
    return true
  } catch {
    return false
  }
}

export async function requestMicrophoneAccess(): Promise<boolean> {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    stream.getTracks().forEach(track => track.stop())

    return true
  } catch {
    return false
  }
}

/**
 * Open Android's camera only after the user explicitly selects Capture photo.
 * The captured bytes stay in memory for the current composer attachment; no
 * image is written to the gallery and no broad photo/storage permission is used.
 */
export async function captureCameraPhoto(): Promise<Blob | null> {
  if (!Capacitor.isNativePlatform()) return null

  try {
    const current = await Camera.checkPermissions()
    const permission = current.camera === 'granted' ? current : await Camera.requestPermissions({ permissions: ['camera'] })
    if (permission.camera !== 'granted') return null

    const photo = await Camera.takePhoto({ correctOrientation: true, quality: 85, saveToGallery: false })
    if (!photo.webPath) return null

    const response = await fetch(photo.webPath)
    if (!response.ok) return null

    const image = await response.blob()
    return image.size > 0 ? image : null
  } catch {
    return null
  }
}

/** Request the explicit camera plus gallery/media grants requested at first connection. */
export async function requestCameraAndGalleryAccess(): Promise<{ camera: boolean; photos: boolean }> {
  if (!Capacitor.isNativePlatform()) return { camera: false, photos: false }

  try {
    const current = await Camera.checkPermissions()
    const permission = current.camera === 'granted'
      ? current
      : await Camera.requestPermissions({ permissions: ['camera'] })
    const media = await MobileCapabilities.requestMedia()

    return {
      camera: permission.camera === 'granted',
      photos: media.granted,
    }
  } catch {
    return { camera: false, photos: false }
  }
}

/**
 * Android has no generic "run in background" permission. This asks the system
 * for a user-controlled battery-optimization exemption, which can improve a
 * resumed remote connection but cannot turn the renderer into a hidden agent or
 * guarantee suspended/terminated app notifications.
 */
export async function requestBackgroundReliabilityPermission(): Promise<boolean> {
  if (!Capacitor.isNativePlatform()) return false

  try {
    const result = await MobileCapabilities.requestBackgroundReliability()
    return result.requested || result.exempt
  } catch {
    return false
  }
}

export async function openExternal(rawUrl: string): Promise<void> {
  try {
    const url = new URL(rawUrl)
    if (url.protocol !== 'https:' || url.username || url.password) return

    await Browser.open({ url: url.toString() })
  } catch {
    /* Invalid or unsupported external URLs are deliberately ignored. */
  }
}

let notifyId = 1
let notificationChannelReady = false

const NOTIFICATION_CHANNELS = {
  activity: {
    id: 'hermes_activity',
    name: 'Hermes activity',
    description: 'Responses and active-session updates from Hermes.',
    importance: 3,
  },
  attention: {
    id: 'hermes_attention',
    name: 'Hermes needs you',
    description: 'Approvals, questions, and errors that need attention.',
    importance: 4,
  },
  background: {
    id: 'hermes_background',
    name: 'Hermes background updates',
    description: 'Non-urgent background task, credit, and plugin updates.',
    importance: 2,
  },
} as const

function notificationChannelFor(kind?: string) {
  if (kind === 'approval' || kind === 'input' || kind === 'turnError') return NOTIFICATION_CHANNELS.attention
  if (kind === 'backgroundDone' || kind === 'credits' || kind === 'plugin') return NOTIFICATION_CHANNELS.background
  return NOTIFICATION_CHANNELS.activity
}

async function ensureNotificationChannel(): Promise<void> {
  if (notificationChannelReady) return

  await Promise.all(
    Object.values(NOTIFICATION_CHANNELS).map(channel =>
      LocalNotifications.createChannel({ ...channel, vibration: channel.id !== NOTIFICATION_CHANNELS.background.id })
    )
  )
  notificationChannelReady = true
}

/** Request the Android prompt only from an explicit settings/test action. */
export async function requestNotificationPermission(): Promise<boolean> {
  if (!Capacitor.isNativePlatform()) return false

  try {
    const current = await LocalNotifications.checkPermissions()
    const permission = current.display === 'granted' ? current : await LocalNotifications.requestPermissions()
    if (permission.display !== 'granted') return false

    await ensureNotificationChannel()
    return true
  } catch {
    return false
  }
}

export interface InitialMobilePermissionResult {
  backgroundReliabilityRequested: boolean
  camera: boolean
  microphone: boolean
  notifications: boolean
  photos: boolean
}

/** Issue the user's first-connection capability requests in a deliberate order. */
export async function requestInitialMobilePermissions(): Promise<InitialMobilePermissionResult> {
  const notifications = await requestNotificationPermission()
  const media = await requestCameraAndGalleryAccess()
  const microphone = await requestMicrophoneAccess()
  const backgroundReliabilityRequested = await requestBackgroundReliabilityPermission()

  return {
    backgroundReliabilityRequested,
    camera: media.camera,
    microphone,
    notifications,
    photos: media.photos,
  }
}

export async function notify(payload: HermesNotification): Promise<boolean> {
  if (!Capacitor.isNativePlatform()) return false
  try {
    const permission = await LocalNotifications.checkPermissions()
    if (permission.display !== 'granted') return false

    await ensureNotificationChannel()
    await LocalNotifications.schedule({
      notifications: [
        {
          id: notifyId++,
          title: payload.title ?? 'Hermes',
          body: payload.body ?? '',
          silent: payload.silent,
          channelId: notificationChannelFor(payload.kind).id,
          foreground: true,
          // This is an immediate notification, not an alarm. Avoid triggering
          // Android's unrelated "Alarms & reminders" permission prompt.
          isExactNotification: false,
          extra: { sessionId: payload.sessionId, kind: payload.kind },
        },
      ],
    })
    return true
  } catch {
    return false
  }
}

/**
 * Reconnect the gateway client when Android resumes or a foreground app regains
 * network connectivity. The Desktop boot hook already serializes reconnects;
 * this bridge only supplies the native lifecycle signals it cannot see itself.
 */
export function onPowerResume(callback: () => void): () => void {
  const resumeHandle = App.addListener('resume', callback)
  const networkHandle = Network.addListener('networkStatusChange', ({ connected }) => {
    if (connected) callback()
  })

  return () => {
    void resumeHandle.then(handle => handle.remove())
    void networkHandle.then(handle => handle.remove())
  }
}
