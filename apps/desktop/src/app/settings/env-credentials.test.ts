import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getEnvVars = vi.fn()
const setEnvVar = vi.fn()

vi.mock('@/hermes', () => ({
  deleteEnvVar: vi.fn(),
  getEnvVars: (profile?: null | string) => getEnvVars(profile),
  revealEnvVar: vi.fn(),
  setEnvVar: (key: string, value: string, profile?: null | string) => setEnvVar(key, value, profile)
}))

beforeEach(() => {
  getEnvVars.mockResolvedValue({})
  setEnvVar.mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.clearAllMocks()
})

// Regression for #92662: the Providers page calls useEnvCredentials() with no
// argument expecting it to follow the app-wide active profile (per this
// hook's own doc comment), but a bare call must not silently pin every read
// and write to the base profile the way a literal `null` scope does.
describe('useEnvCredentials', () => {
  it('fetches with the active profile (undefined), not the base profile (null), on a bare call', async () => {
    const { useEnvCredentials } = await import('./env-credentials')

    renderHook(() => useEnvCredentials())

    await waitFor(() => expect(getEnvVars).toHaveBeenCalled())
    expect(getEnvVars).toHaveBeenCalledWith(undefined)
  })

  it('saves against the active profile (undefined) on a bare call', async () => {
    const { useEnvCredentials } = await import('./env-credentials')

    const { result } = renderHook(() => useEnvCredentials())

    await result.current.saveValue('LM_API_KEY', 'sk-test')

    expect(setEnvVar).toHaveBeenCalledWith('LM_API_KEY', 'sk-test', undefined)
  })
})
