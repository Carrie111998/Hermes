/**
 * native.ts — the handful of window.hermesDesktop methods that map cleanly onto
 * Capacitor plugins. Everything else is stubbed in stubs.ts.
 */

import { App } from '@capacitor/app'
import { Browser } from '@capacitor/browser'
import { Clipboard } from '@capacitor/clipboard'
import { Capacitor } from '@capacitor/core'
import { LocalNotifications } from '@capacitor/local-notifications'
import { Network } from '@capacitor/network'

import type { HermesNotification } from '@/global'

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
let notifPermissionAsked = false
let notificationChannelReady = false
const NOTIFICATION_CHANNEL_ID = 'hermes_activity'

async function ensureNotificationChannel(): Promise<void> {
  if (notificationChannelReady) return

  await LocalNotifications.createChannel({
    id: NOTIFICATION_CHANNEL_ID,
    name: 'Hermes activity',
    description: 'Completion and attention notices from an active Hermes session.',
    importance: 3,
    vibration: true,
  })
  notificationChannelReady = true
}

export async function notify(payload: HermesNotification): Promise<boolean> {
  if (!Capacitor.isNativePlatform()) return false
  try {
    if (!notifPermissionAsked) {
      notifPermissionAsked = true
      const perm = await LocalNotifications.checkPermissions()
      if (perm.display !== 'granted') {
        await LocalNotifications.requestPermissions()
      }
    }
    await ensureNotificationChannel()
    await LocalNotifications.schedule({
      notifications: [
        {
          id: notifyId++,
          title: payload.title ?? 'Hermes',
          body: payload.body ?? '',
          silent: payload.silent,
          channelId: NOTIFICATION_CHANNEL_ID,
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
