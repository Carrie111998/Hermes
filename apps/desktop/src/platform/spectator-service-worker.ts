import { isBrowserSpectator } from './browser-spectator'

declare global {
  interface WindowEventMap {
    'hermes:spectator-update-ready': CustomEvent<ServiceWorkerRegistration>
  }
}

function spectatorBasePath(): string {
  const raw = window.__HERMES_SPECTATOR_BASE_PATH__?.trim() ?? ''
  return raw ? `/${raw.replace(/^\/+|\/+$/g, '')}` : ''
}

export function spectatorServiceWorkerUrl(): string {
  return `${spectatorBasePath()}/spectator-sw.js`
}

export async function registerSpectatorServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!isBrowserSpectator() || !('serviceWorker' in navigator)) return null

  const registration = await navigator.serviceWorker.register(spectatorServiceWorkerUrl(), {
    scope: `${spectatorBasePath() || ''}/`
  })

  const announceWaitingWorker = () => {
    if (!registration.waiting) return
    window.dispatchEvent(new CustomEvent('hermes:spectator-update-ready', { detail: registration }))
  }

  announceWaitingWorker()
  registration.addEventListener('updatefound', () => {
    const worker = registration.installing
    if (!worker) return

    worker.addEventListener('statechange', () => {
      if (worker.state === 'installed' && navigator.serviceWorker.controller) announceWaitingWorker()
    })
  })

  return registration
}
