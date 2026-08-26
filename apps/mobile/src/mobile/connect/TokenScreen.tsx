import { useState } from 'react'

import type { ProbeResult } from '~bridge/auth'
import { SecureStoreError } from '~bridge/secure-store'

import { Brand, Button, Card, ErrorNote, Field, Screen } from '../ui'

/**
 * Token-auth gateways (auth_required:false but not open) need a static session
 * token — the gateway rejects an empty `?token=`/`X-Hermes-Session-Token`, and
 * the desktop likewise refuses a token-mode config with no saved token. So we
 * collect one here before committing the target, rather than connecting blind.
 */
export function TokenScreen({
  probe,
  onBack,
  onToken,
  onTransientToken,
}: {
  probe: ProbeResult
  onBack: () => void
  onToken: (token: string) => Promise<void>
  onTransientToken: (token: string) => void
}) {
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [canContinueWithoutSaving, setCanContinueWithoutSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit() {
    const value = token.trim()
    if (!value || busy) {
      if (!value) setError('A session token is required for this gateway.')
      return
    }

    setBusy(true)
    setError(null)
    setCanContinueWithoutSaving(false)
    try {
      await onToken(value)
    } catch (reason) {
      const message = (reason as Error).message
      if (reason instanceof SecureStoreError) {
        setError(`Could not save the gateway token: ${message}`)
        setCanContinueWithoutSaving(true)
      } else {
        setError(`Could not connect to the gateway: ${message}`)
      }
    } finally {
      setBusy(false)
    }
  }

  function connectWithoutSaving() {
    const value = token.trim()
    if (!value || busy) return
    onTransientToken(value)
  }

  return (
    <Screen>
      <Brand subtitle={hostLabel(probe.baseUrl)} />
      <Card>
        <Field
          label="Session token"
          type="password"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && submit()}
        />
        <ErrorNote>{error}</ErrorNote>
        <Button busy={busy} onClick={() => void submit()}>
          Connect
        </Button>
        {canContinueWithoutSaving && (
          <Button onClick={connectWithoutSaving} variant="ghost">
            Connect for this session only
          </Button>
        )}
        <Button variant="ghost" onClick={onBack}>
          Use a different gateway
        </Button>
      </Card>
      <p className="px-1 text-center text-xs text-muted-foreground/80">
        This gateway uses token auth. Paste the same session token your desktop
        uses under Settings &rarr; Gateway. Session-only connection keeps it only
        in memory and asks again after the app closes.
      </p>
    </Screen>
  )
}

function hostLabel(baseUrl: string): string {
  try {
    return new URL(baseUrl).host
  } catch {
    return baseUrl
  }
}
