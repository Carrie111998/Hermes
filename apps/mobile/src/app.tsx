import { App } from '@capacitor/app'
import { Preferences } from '@capacitor/preferences'
import { useStore } from '@nanostores/react'
import { type ReactNode, useEffect, useRef, useState } from 'react'

// The REAL desktop app (DesktopController) — its AppShell already collapses to a
// drawer on narrow viewports, so mounting it on a phone gives the desktop chat
// experience, responsively. We only gate it behind connect/login.
import DesktopController from '@/app'

import { verifyTokenGateway, type ProbeResult } from '~bridge/auth'
import { enableMobileBackgroundSession, requestInitialMobilePermissions } from '~bridge/native'
import { $reauthNonce, loadTarget, setTarget, setTransientTarget, type GatewayTarget } from '~bridge/state'

import { ConnectScreen } from '~mobile/connect/ConnectScreen'
import { LoginScreen } from '~mobile/connect/LoginScreen'
import { TokenScreen } from '~mobile/connect/TokenScreen'
import { MobileBehaviors } from '~mobile/mobile-behaviors'
import { INITIAL_MOBILE_CAPABILITIES_KEY, shouldRequestInitialMobileCapabilities } from '~mobile/initial-capabilities'
import { MobileRootShell } from '~mobile/mobile-root-shell'
import { mobileBackDestination } from '~mobile/navigation'
import { initNativeChrome } from '~mobile/native-init'

type View = 'loading' | 'connect' | 'login' | 'token' | 'connected'

export function MobileRoot() {
  const [view, setView] = useState<View>('loading')
  const [probe, setProbe] = useState<ProbeResult | null>(null)
  const initialCapabilityRequest = useRef(false)
  const reauthNonce = useStore($reauthNonce)

  // Boot: configure native chrome (status bar), then restore a saved gateway
  // target, else show the connect screen.
  useEffect(() => {
    void initNativeChrome()
    void (async () => {
      const t = await loadTarget()
      setView(t ? 'connected' : 'connect')
    })()
  }, [])

  // Setup screens precede DesktopController, so they own their Android Back
  // behavior: login/token returns to gateway selection; selection exits.
  useEffect(() => {
    if (view === 'loading' || view === 'connected') return

    let disposed = false
    let handle: { remove: () => Promise<void> } | undefined
    void App.addListener('backButton', () => {
      const destination = mobileBackDestination(view)
      if (destination) setView(destination)
      else void App.exitApp()
    }).then(listener => {
      if (disposed) {
        void listener.remove()
      } else {
        handle = listener
      }
    })
    return () => {
      disposed = true
      void handle?.remove()
    }
  }, [view])

  // A 401 demanding re-login bounces us back to the right entry form for the
  // gateway's auth mode (token gateways re-collect the token, not a password).
  useEffect(() => {
    if (reauthNonce > 0 && probe) setView(probe.authMode === 'token' ? 'token' : 'login')
  }, [reauthNonce, probe])

  // Steven explicitly requested first-connection capability onboarding. The
  // non-secret marker prevents repeated permission prompts after this first
  // chance; Android itself remains the decision-maker for every grant.
  useEffect(() => {
    if (view !== 'connected' || initialCapabilityRequest.current) return

    initialCapabilityRequest.current = true
    void (async () => {
      let requestInitialCapabilities = true
      try {
        const marker = await Preferences.get({ key: INITIAL_MOBILE_CAPABILITIES_KEY })
        requestInitialCapabilities = shouldRequestInitialMobileCapabilities(marker.value)
      } catch {
        // A failed non-secret marker must not stop the explicitly requested
        // first-connection prompts. The in-memory ref still prevents duplicates.
      }

      if (requestInitialCapabilities) {
        await requestInitialMobilePermissions()
        try {
          await Preferences.set({ key: INITIAL_MOBILE_CAPABILITIES_KEY, value: 'requested' })
        } catch {
          // Marker persistence is convenience only; native permission state stays
          // authoritative even if this write fails.
        }
      }

      // This only arms Android's visible foreground-service path for the next
      // background transition; it never starts a hidden local agent.
      await enableMobileBackgroundSession()
    })()
  }, [view])

  async function onProbeResult(p: ProbeResult) {
    setProbe(p)
    if (p.authMode === 'token') {
      // Token gateways need a static session token; collect it before committing
      // (an empty token is rejected by both the gateway and desktop parity).
      setView('token')
      return
    }
    if (!p.needsLogin) {
      const provider = p.providers.find((x) => x.supportsPassword)?.name ?? 'basic'
      await commit({ baseUrl: p.baseUrl, authMode: 'oauth', provider })
      return
    }
    setView('login')
  }

  async function commit(t: GatewayTarget) {
    await setTarget(t)
    setView('connected')
  }

  async function commitToken(t: GatewayTarget) {
    await verifyTokenGateway(t.baseUrl, t.token ?? '')
    await commit(t)
  }

  function connectWithoutSaving(t: GatewayTarget) {
    setTransientTarget(t)
    setView('connected')
  }

  let content: ReactNode
  if (view === 'loading') {
    content = (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Loading…
      </div>
    )
  } else if (view === 'connect') {
    content = <ConnectScreen initialUrl={probe?.baseUrl ?? ''} onResult={onProbeResult} />
  } else if (view === 'login' && probe) {
    content = (
      <LoginScreen
        probe={probe}
        onBack={() => setView('connect')}
        onLoggedIn={(provider) => commit({ baseUrl: probe.baseUrl, authMode: 'oauth', provider })}
      />
    )
  } else if (view === 'token' && probe) {
    content = (
      <TokenScreen
        probe={probe}
        onBack={() => setView('connect')}
        onToken={(token) =>
          commitToken({ baseUrl: probe.baseUrl, authMode: 'token', provider: null, token })
        }
        onTransientToken={(token) =>
          connectWithoutSaving({ baseUrl: probe.baseUrl, authMode: 'token', provider: null, token })
        }
      />
    )
  } else {
    // Connected → hand off to the real desktop chat app, plus the mobile touch
    // adaptations (toolbar, drawers, settings master-detail, Android Back).
    content = (
      <>
        <DesktopController />
        <MobileBehaviors />
      </>
    )
  }

  // One owned shell is a critical structural boundary: the mobile CSS may size
  // this shell to the safe viewport without accidentally stretching each toolbar
  // button into its own full-height rail.
  return <MobileRootShell>{content}</MobileRootShell>
}
