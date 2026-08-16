import * as QRCode from 'qrcode'
import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { ErrorBanner } from '@/components/ui/error-state'
import { Input } from '@/components/ui/input'
import {
  applyWhatsAppOnboarding,
  cancelWhatsAppOnboarding,
  getWhatsAppOnboardingStatus,
  type MessagingPlatformInfo,
  startWhatsAppOnboarding,
  type WhatsAppOnboardingMode,
  type WhatsAppOnboardingResponse
} from '@/hermes'
import { Loader2, Save } from '@/lib/icons'
import { notify, notifyError } from '@/store/notifications'

interface WhatsAppOnboardingProps {
  onChanged: () => Promise<void>
  platform: MessagingPlatformInfo
  profile: string
}

const TERMINAL_STATUSES = new Set(['connected', 'error', 'expired', 'cancelled'])

const errorMessage = (error: unknown) => (error instanceof Error ? error.message : String(error))

const isTerminalPollingError = (error: unknown) => {
  const message = errorMessage(error)

  return /\b410\b/.test(message) && /\b(expired|gone)\b/i.test(message)
}

export function WhatsAppOnboarding({ onChanged, platform, profile }: WhatsAppOnboardingProps) {
  const configuredMode = platform.whatsapp_setup?.mode
  const [mode, setMode] = useState<WhatsAppOnboardingMode>(configuredMode || 'bot')
  const [allowedUsers, setAllowedUsers] = useState('')
  const [setup, setSetup] = useState<WhatsAppOnboardingResponse | null>(null)
  const [qrDataUrl, setQrDataUrl] = useState('')
  const [error, setError] = useState('')
  const [starting, setStarting] = useState(false)
  const [applying, setApplying] = useState(false)
  const activePairingId = useRef<string | null>(null)
  const lifecycleGeneration = useRef(0)

  useEffect(() => {
    if (!setup && configuredMode) {
      setMode(configuredMode)
    }
  }, [configuredMode, setup])

  // Invalidates request generations on teardown; this does not mirror reactive state.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    return () => {
      lifecycleGeneration.current += 1
      const currentPairingId = activePairingId.current
      activePairingId.current = null

      if (currentPairingId) {
        void cancelWhatsAppOnboarding(currentPairingId, profile).catch(() => undefined)
      }
    }
  }, [profile])

  const renderQr = useCallback(async (pairingId: string, payload: string) => {
    if (!payload) {
      return
    }

    const svg = await QRCode.toString(payload, {
      errorCorrectionLevel: 'M',
      margin: 3,
      type: 'svg',
      width: 240
    })

    if (activePairingId.current !== pairingId) {
      return
    }

    setQrDataUrl(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`)
  }, [])

  const pairingId = setup?.pairing_id ?? null
  const setupExpiresAt = setup?.expires_at ?? ''
  const setupQrPayload = setup?.qr_payload ?? ''
  const setupStatus = setup?.status ?? null

  useEffect(() => {
    if (!pairingId || (setupStatus && TERMINAL_STATUSES.has(setupStatus))) {
      return
    }

    let cancelled = false
    let timeout: ReturnType<typeof setTimeout> | null = null

    const poll = async () => {
      try {
        const status = await getWhatsAppOnboardingStatus(pairingId, profile)

        if (cancelled || activePairingId.current !== pairingId) {
          return
        }

        if (status.qr_payload && status.qr_payload !== setupQrPayload) {
          await renderQr(pairingId, status.qr_payload)
        }

        if (cancelled || activePairingId.current !== pairingId) {
          return
        }

        if (status.status === 'cancelled' || status.status === 'expired') {
          setSetup(null)
          setQrDataUrl('')
          setError(
            status.error ||
              (status.status === 'expired'
                ? 'WhatsApp QR setup expired. Start a new QR setup to try again.'
                : 'WhatsApp setup was cancelled. Start a new QR setup to try again.')
          )

          return
        }

        setSetup(status)

        if (status.status === 'error') {
          setError(status.error || 'WhatsApp pairing failed.')

          return
        }

        if (TERMINAL_STATUSES.has(status.status)) {
          setError('')

          return
        }

        setError('')
        timeout = setTimeout(poll, 1500)
      } catch (pollError) {
        if (cancelled || activePairingId.current !== pairingId) {
          return
        }

        const expiresAt = Date.parse(setupExpiresAt)
        const expired = Number.isFinite(expiresAt) && Date.now() >= expiresAt

        if (isTerminalPollingError(pollError) || expired) {
          setSetup(null)
          setQrDataUrl('')
          setError('WhatsApp QR setup expired. Start a new QR setup to try again.')

          return
        }

        setError(`Still waiting for WhatsApp. Retrying after: ${errorMessage(pollError)}`)
        timeout = setTimeout(poll, 2000)
      }
    }

    timeout = setTimeout(poll, 1000)

    return () => {
      cancelled = true

      if (timeout) {
        clearTimeout(timeout)
      }
    }
  }, [pairingId, profile, renderQr, setupExpiresAt, setupQrPayload, setupStatus])

  const reset = () => {
    activePairingId.current = null
    setSetup(null)
    setQrDataUrl('')
    setError('')
    setStarting(false)
    setApplying(false)
  }

  const start = async () => {
    const generation = lifecycleGeneration.current
    setStarting(true)
    setError('')
    setQrDataUrl('')

    try {
      const response = await startWhatsAppOnboarding({ allowed_users: allowedUsers.trim(), mode }, profile)

      if (lifecycleGeneration.current !== generation) {
        void cancelWhatsAppOnboarding(response.pairing_id, profile).catch(() => undefined)

        return
      }

      activePairingId.current = response.pairing_id
      setSetup(response)

      if (response.qr_payload) {
        await renderQr(response.pairing_id, response.qr_payload)
      }

      if (response.status === 'error') {
        setError(response.error || 'WhatsApp pairing failed.')
      }
    } catch (startError) {
      if (lifecycleGeneration.current !== generation) {
        return
      }

      activePairingId.current = null
      setSetup(null)
      setError(errorMessage(startError))
    } finally {
      if (lifecycleGeneration.current === generation) {
        setStarting(false)
      }
    }
  }

  const cancel = async () => {
    const currentPairingId = activePairingId.current
    activePairingId.current = null

    if (currentPairingId) {
      try {
        await cancelWhatsAppOnboarding(currentPairingId, profile)
      } catch {
        // The backend session expires on its own; local cancellation still wins.
      }
    }

    reset()
  }

  const apply = async () => {
    if (!setup || setup.status !== 'connected') {
      return
    }

    const generation = lifecycleGeneration.current
    setApplying(true)
    setError('')

    try {
      const result = await applyWhatsAppOnboarding(
        setup.pairing_id,
        {
          allowed_users: allowedUsers.trim(),
          mode
        },
        profile
      )

      if (lifecycleGeneration.current !== generation) {
        return
      }

      reset()
      await onChanged()
      notify({
        kind: result.restart_started ? 'success' : 'warning',
        title: 'WhatsApp setup saved',
        message: result.restart_started
          ? 'The gateway is restarting with the linked account.'
          : result.restart_error || 'Restart the gateway to finish connecting WhatsApp.'
      })
    } catch (applyError) {
      if (lifecycleGeneration.current !== generation) {
        return
      }

      setApplying(false)
      setError(errorMessage(applyError))
      notifyError(applyError, 'Failed to save WhatsApp setup')
    }
  }

  const connected = setup?.status === 'connected'
  const waiting = Boolean(setup && !connected && setup.status !== 'error' && setup.status !== 'expired')

  const linkedAccount = setup?.account_phone
    ? `+${setup.account_phone}`
    : setup?.account_name || setup?.account_id || 'WhatsApp device linked'

  return (
    <section className="rounded-md border border-border bg-(--ui-row-hover-background) p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h4 className="text-sm font-semibold">Pair WhatsApp</h4>
          <p className="mt-1 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
            Link the WhatsApp account Hermes should use. The QR payload stays inside this app.
          </p>
        </div>
        {!setup && (
          <Button disabled={starting} onClick={() => void start()} size="sm" variant="secondary">
            {starting && <Loader2 className="animate-spin" />}
            {starting ? 'Starting…' : 'Pair with QR'}
          </Button>
        )}
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-[auto_minmax(12rem,1fr)] sm:items-end">
        <div>
          <div className="mb-1 text-xs font-medium text-(--ui-text-secondary)">Mode</div>
          <div className="flex gap-1">
            {(['bot', 'self-chat'] as const).map(value => (
              <Button
                aria-pressed={mode === value}
                disabled={Boolean(setup)}
                key={value}
                onClick={() => setMode(value)}
                size="sm"
                variant={mode === value ? 'secondary' : 'ghost'}
              >
                {value === 'bot' ? 'Bot account' : 'Self-chat'}
              </Button>
            ))}
          </div>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-(--ui-text-secondary)" htmlFor="whatsapp-allowed-users">
            Allowed numbers
          </label>
          <Input
            disabled={Boolean(setup)}
            id="whatsapp-allowed-users"
            onChange={event => setAllowedUsers(event.target.value)}
            placeholder="15551234567,15557654321"
            value={allowedUsers}
          />
        </div>
      </div>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      {setup && (
        <div className="mt-3 grid gap-3 sm:grid-cols-[minmax(0,1fr)_240px]">
          <div className="min-w-0 text-sm">
            {connected ? (
              <>
                <div className="font-medium text-foreground">Linked as {linkedAccount}</div>
                <p className="mt-1 text-(--ui-text-tertiary)">
                  Save the session and restart the gateway to finish setup.
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button disabled={applying} onClick={() => void apply()} size="sm">
                    {applying ? <Loader2 className="animate-spin" /> : <Save />}
                    {applying ? 'Saving…' : 'Save and restart'}
                  </Button>
                  <Button disabled={applying} onClick={() => void cancel()} size="sm" variant="ghost">
                    Cancel
                  </Button>
                </div>
              </>
            ) : (
              <>
                <div className="font-medium text-foreground">
                  {setup.status === 'installing' ? 'Preparing WhatsApp bridge…' : 'Waiting for WhatsApp…'}
                </div>
                <p className="mt-1 text-(--ui-text-tertiary)">
                  Open WhatsApp on the account being linked, then choose Linked Devices → Link a device.
                </p>
                <Button className="mt-3" onClick={() => void cancel()} size="sm" variant="ghost">
                  Cancel
                </Button>
              </>
            )}
          </div>

          <div className="grid place-items-center gap-2">
            {qrDataUrl ? (
              <img
                alt="WhatsApp setup QR code"
                className="size-60 bg-white p-2"
                height={240}
                src={qrDataUrl}
                width={240}
              />
            ) : connected ? (
              <div className="grid size-60 place-items-center border border-border bg-background/45 p-4 text-center text-sm text-(--ui-text-tertiary)">
                {linkedAccount}
              </div>
            ) : (
              <div className="grid size-60 place-items-center border border-border bg-background/45 p-4 text-center text-xs text-(--ui-text-tertiary)">
                <span>
                  <Loader2 className="mx-auto mb-2 animate-spin" />
                  Waiting for WhatsApp to provide a QR code…
                </span>
              </div>
            )}
            {waiting && (
              <span className="text-center text-xs text-(--ui-text-tertiary)">
                Scan with WhatsApp Linked Devices, not the camera app.
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  )
}
