import { beforeEach, expect, test, vi } from 'vitest'

import { $notifications, clearNotifications, notifyError } from './notifications'

beforeEach(() => {
  clearNotifications()
})

function lastMessage(): string {
  return $notifications.get()[0]?.message ?? ''
}

// Regression for #39365: a gateway auth 401 (bad API_SERVER_KEY) must not be
// summarized as a provider (OpenAI/OpenRouter) API key problem.
test('gateway_auth_failed error is summarized as gateway auth, not provider key', () => {
  notifyError(
    new Error(
      '401 {"error": {"message": "Invalid gateway API key (API_SERVER_KEY)", "type": "gateway_auth_error", "code": "gateway_auth_failed"}}'
    ),
    'Request failed'
  )

  expect(lastMessage()).toContain('API_SERVER_KEY')
  expect(lastMessage()).not.toMatch(/OpenAI/i)
})

test('provider invalid_api_key error still maps to the OpenAI summary', () => {
  notifyError(
    new Error('401 {"error": {"message": "Incorrect API key provided", "code": "invalid_api_key"}}'),
    'Request failed'
  )

  expect(lastMessage()).toMatch(/OpenAI rejected the API key/i)
})

test('recoverable STT errors surface the ID and a copyable recovery command', () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText }
  })
  const recoveryId = 'a'.repeat(32)

  notifyError(
    new Error(
      `400: ${JSON.stringify({
        detail: `Transcription failed. On the backend run hermes stt recovery retry ${recoveryId}.`,
        error_code: 'provider_error',
        recovery_available: true,
        recovery_id: recoveryId
      })}`
    ),
    'Voice transcription failed'
  )

  const notification = $notifications.get()[0]
  expect(notification?.message).toContain(recoveryId)
  expect(notification?.message).not.toBe('Voice transcription failed')
  expect(notification?.action?.label).toMatch(/recovery command/i)
  notification?.action?.onClick()
  expect(writeText).toHaveBeenCalledWith(`hermes stt recovery retry ${recoveryId}`)
})

test('recoverable STT errors copy the owning profile in the recovery command', () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText }
  })
  const recoveryId = 'b'.repeat(32)

  notifyError(
    new Error(
      `400: ${JSON.stringify({
        detail: `Transcription failed. Run hermes -p coder stt recovery retry ${recoveryId}.`,
        error_code: 'provider_error',
        recovery_available: true,
        recovery_id: recoveryId,
        recovery_profile: 'coder'
      })}`
    ),
    'Voice transcription failed'
  )

  $notifications.get()[0]?.action?.onClick()
  expect(writeText).toHaveBeenCalledWith(`hermes -p coder stt recovery retry ${recoveryId}`)
})
