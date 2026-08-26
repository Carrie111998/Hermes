import { Preferences } from '@capacitor/preferences'
import { Capacitor, registerPlugin } from '@capacitor/core'

interface SecureStorePlugin {
  get(options: { key: string }): Promise<{ value: null | string }>
  remove(options: { key: string }): Promise<void>
  set(options: { key: string; value: string }): Promise<void>
}

const SecureStore = registerPlugin<SecureStorePlugin>('SecureStore')

/** A native Keystore operation failed. Its message never contains the value. */
export class SecureStoreError extends Error {
  constructor(operation: 'read' | 'persist' | 'clear', cause: unknown) {
    const detail = cause instanceof Error ? cause.message : 'Native secure storage failed.'
    super(`Could not ${operation} secure mobile storage: ${detail}`)
    this.name = 'SecureStoreError'
  }
}

function useNativeSecureStore(): boolean {
  return Capacitor.isNativePlatform() && Capacitor.getPlatform() === 'android'
}

export async function secureGet(key: string): Promise<null | string> {
  if (useNativeSecureStore()) {
    try {
      return (await SecureStore.get({ key })).value
    } catch (error) {
      throw new SecureStoreError('read', error)
    }
  }

  return (await Preferences.get({ key })).value
}

export async function secureSet(key: string, value: string): Promise<void> {
  if (useNativeSecureStore()) {
    try {
      await SecureStore.set({ key, value })
    } catch (error) {
      throw new SecureStoreError('persist', error)
    }
    return
  }

  await Preferences.set({ key, value })
}

export async function secureRemove(key: string): Promise<void> {
  if (useNativeSecureStore()) {
    try {
      await SecureStore.remove({ key })
    } catch (error) {
      throw new SecureStoreError('clear', error)
    }
    return
  }

  await Preferences.remove({ key })
}
