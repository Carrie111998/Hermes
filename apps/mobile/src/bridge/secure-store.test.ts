import { beforeEach, describe, expect, it, vi } from 'vitest'

const { secureStore } = vi.hoisted(() => ({
  secureStore: {
    get: vi.fn(),
    remove: vi.fn(),
    set: vi.fn()
  }
}))

vi.mock('@capacitor/core', () => ({
  Capacitor: { getPlatform: () => 'android', isNativePlatform: () => true },
  registerPlugin: () => secureStore
}))

import { SecureStoreError, secureGet, secureRemove, secureSet } from './secure-store'

describe('Android secure store', () => {
  beforeEach(() => {
    secureStore.get.mockReset()
    secureStore.remove.mockReset()
    secureStore.set.mockReset()
  })

  it('delegates a stored token to the native secure store', async () => {
    secureStore.get.mockResolvedValue({ value: 'ciphertext-backed-token' })

    await expect(secureGet('hermes.target')).resolves.toBe('ciphertext-backed-token')
    expect(secureStore.get).toHaveBeenCalledWith({ key: 'hermes.target' })
  })

  it('stores and removes target records through the native bridge', async () => {
    await secureSet('hermes.target', 'value')
    await secureRemove('hermes.target')

    expect(secureStore.set).toHaveBeenCalledWith({ key: 'hermes.target', value: 'value' })
    expect(secureStore.remove).toHaveBeenCalledWith({ key: 'hermes.target' })
  })

  it('classifies native persistence failure without exposing the stored value', async () => {
    secureStore.set.mockRejectedValueOnce(new Error('InvalidAlgorithmParameterException'))

    await expect(secureSet('hermes.target', 'secret-token')).rejects.toBeInstanceOf(SecureStoreError)
  })
})
