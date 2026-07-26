import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef } from 'react'

import type { HermesGateway } from '@/hermes'
import {
  $gateway,
  ensureActiveGatewayOpen,
  isActivePrimary,
  reconnectPrimaryGateway,
  takeGatewayReauthError
} from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $gatewayState } from '@/store/session'

export function useGatewayRequest() {
  const gatewayState = useStore($gatewayState)
  const gatewayRef = useRef<HermesGateway | null>(null)

  const connectionRef = useRef<Awaited<ReturnType<NonNullable<typeof window.hermesDesktop>['getConnection']>> | null>(
    null
  )

  const gatewayStateRef = useRef(gatewayState)

  useEffect(() => {
    gatewayStateRef.current = gatewayState
  }, [gatewayState])

  // Track the active gateway (primary or a background profile's socket) so
  // outbound requests and overlay props always target the focused profile.
  useEffect(
    () =>
      $gateway.subscribe(gateway => {
        gatewayRef.current = gateway as HermesGateway | null
      }),
    []
  )

  // Recover a dropped active PRIMARY gateway. The OAuth-aware reconnect now
  // lives in the gateway registry (reconnectPrimaryGateway): the profile is
  // explicit, concurrent callers are deduped, the connection is published as
  // foreground only when this profile is active, and the reauth error is stored
  // per profile. This hook is a thin caller that mirrors the published
  // connection back into connectionRef for its consumers.
  const ensureGatewayOpen = useCallback(async () => {
    const existing = gatewayRef.current

    if (!existing) {
      return null
    }

    if (gatewayStateRef.current === 'open') {
      return existing
    }

    const recovered = await reconnectPrimaryGateway($activeGatewayProfile.get())
    connectionRef.current = $connection.get()

    return recovered
  }, [])

  const requestGateway = useCallback(
    async <T>(method: string, params: Record<string, unknown> = {}, timeoutMs?: number, signal?: AbortSignal) => {
      const gateway = gatewayRef.current

      if (!gateway) {
        throw new Error('Hermes gateway unavailable')
      }

      try {
        return await gateway.request<T>(method, params, timeoutMs, signal)
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)

        if (!/not connected|connection closed/i.test(message)) {
          throw error
        }

        // Primary keeps the OAuth-aware reconnect (remote gateways re-mint a
        // single-use ticket); background profiles recover through the registry's
        // bounded secondary reconnect.
        const recovered = isActivePrimary() ? await ensureGatewayOpen() : await ensureActiveGatewayOpen()

        if (!recovered) {
          // Prefer the reauth error from the failed reconnect (OAuth session
          // expired) over the generic transport error that triggered the retry.
          const reauthError = takeGatewayReauthError($activeGatewayProfile.get())

          if (reauthError) {
            throw reauthError
          }

          throw error
        }

        return recovered.request<T>(method, params, timeoutMs, signal)
      }
    },
    [ensureGatewayOpen]
  )

  return { connectionRef, gatewayRef, requestGateway }
}
