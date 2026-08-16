// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { MessagingPlatformInfo } from '@/types/hermes'

const getMessagingPlatforms = vi.fn()
const updateMessagingPlatform = vi.fn()
const startWhatsAppOnboarding = vi.fn()
const getWhatsAppOnboardingStatus = vi.fn()
const applyWhatsAppOnboarding = vi.fn()
const cancelWhatsAppOnboarding = vi.fn()
const getPairing = vi.fn()
const approvePairing = vi.fn()
const revokePairing = vi.fn()
const openExternalLink = vi.fn()

vi.mock('@/hermes', () => ({
  applyWhatsAppOnboarding: (pairingId: string, body: unknown, profile?: string) =>
    applyWhatsAppOnboarding(pairingId, body, profile),
  approvePairing: (platformId: string, requestId: string) => approvePairing(platformId, requestId),
  cancelWhatsAppOnboarding: (pairingId: string, profile?: string) => cancelWhatsAppOnboarding(pairingId, profile),
  getMessagingPlatforms: (profile?: string) => getMessagingPlatforms(profile),
  getPairing: (profile?: string) => getPairing(profile),
  getWhatsAppOnboardingStatus: (pairingId: string, profile?: string) => getWhatsAppOnboardingStatus(pairingId, profile),
  revokePairing: (platformId: string, userId: string) => revokePairing(platformId, userId),
  startWhatsAppOnboarding: (body: unknown, profile?: string) => startWhatsAppOnboarding(body, profile),
  updateMessagingPlatform: (id: string, body: unknown) => updateMessagingPlatform(id, body)
}))

vi.mock('@/store/profile', async () => {
  const { atom } = await import('nanostores')

  return { $activeGatewayProfile: atom('default') }
})

vi.mock('qrcode', () => ({
  toString: vi.fn().mockResolvedValue('<svg xmlns="http://www.w3.org/2000/svg"><path /></svg>')
}))

vi.mock('@/lib/external-link', () => ({
  openExternalLink: (href: string) => openExternalLink(href)
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/store/system-actions', () => ({
  runGatewayRestart: vi.fn()
}))

function platform(patch: Partial<MessagingPlatformInfo> = {}): MessagingPlatformInfo {
  return {
    configured: false,
    description: 'A platform.',
    docs_url: '',
    enabled: false,
    env_vars: [],
    gateway_running: true,
    id: 'teams',
    name: 'Microsoft Teams',
    state: 'disabled',
    ...patch
  }
}

beforeEach(() => {
  updateMessagingPlatform.mockResolvedValue({ ok: true, platform: 'teams' })
  cancelWhatsAppOnboarding.mockResolvedValue({ ok: true })
  getPairing.mockResolvedValue({ approved: [], pending: [] })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function renderMessaging() {
  const { MessagingView } = await import('./index')
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <MemoryRouter>
        <MessagingView />
      </MemoryRouter>
    )
  })

  return result!
}

describe('MessagingView setup-guide link', () => {
  it('hides the setup-guide button for a plugin platform with no docs URL', async () => {
    // Teams (and other plugin platforms) ship an empty docs_url. Rendering an
    // anchor with href="" let Electron resolve it to the app's own packaged
    // index.html and fail with an OS "file not found" dialog. The button must
    // simply not appear when there is no guide to open.
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform({ docs_url: '' })] })

    await renderMessaging()

    expect((await screen.findAllByText('Microsoft Teams')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Open setup guide')).toBeNull()
  })

  it('opens a real docs URL through the validated external opener', async () => {
    const docsUrl = 'https://hermes-agent.nousresearch.com/docs/user-guide/messaging/teams'
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform({ docs_url: docsUrl })] })

    await renderMessaging()

    const link = await screen.findByText('Open setup guide')
    await act(async () => {
      fireEvent.click(link)
    })

    await waitFor(() => expect(openExternalLink).toHaveBeenCalledWith(docsUrl))
  })
})

describe('MessagingView pairing', () => {
  const pendingUser = {
    age_minutes: 3,
    platform: 'teams',
    request_id: 'a1b2c3d4e5f60718',
    user_id: '7712345',
    user_name: 'Bee'
  }

  it('approves the listed request by its request id, never by a code', async () => {
    // The whole point of the request-id grant path: the UI can only ever send
    // the server-side row id, because the one-time code is never returned by
    // the API. Posting anything derived from the code could not be approved.
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockResolvedValue({ approved: [], pending: [pendingUser] })
    approvePairing.mockResolvedValue({ ok: true, user: { user_id: '7712345', user_name: 'Bee' } })

    await renderMessaging()

    const approve = await screen.findByRole('button', { name: 'Approve' })
    await act(async () => {
      fireEvent.click(approve)
    })

    await waitFor(() => expect(approvePairing).toHaveBeenCalledWith('teams', 'a1b2c3d4e5f60718'))
  })

  it('restores the pending row when approval fails', async () => {
    // Optimistic removal must not silently swallow the request: a failed
    // approve has to leave the operator something to retry.
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockResolvedValue({ approved: [], pending: [pendingUser] })
    approvePairing.mockRejectedValue(new Error('500 boom'))

    await renderMessaging()

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    })

    expect(await screen.findByRole('button', { name: 'Approve' })).toBeTruthy()
    expect(screen.getByText('Bee')).toBeTruthy()
  })

  it('shows no pairing affordance when nobody is waiting', async () => {
    // Approvals are rare; an always-present empty state would be permanent
    // chrome on a page that is otherwise about credentials.
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockResolvedValue({ approved: [], pending: [] })

    await renderMessaging()

    expect((await screen.findAllByText('Microsoft Teams')).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.queryByText(/Pending requests/)).toBeNull()
  })

  it('still renders platforms when the pairing endpoint fails', async () => {
    // An older backend without the endpoint must not blank the page.
    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockRejectedValue(new Error('404 not found'))

    await renderMessaging()

    expect((await screen.findAllByText('Microsoft Teams')).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
  })

  it('refetches pending rows on pairing.changed, not on platforms.changed', async () => {
    // The two signals are not interchangeable: platforms.changed tracks
    // connect/disconnect health via gateway_state.json, which a new pairing
    // request never moves. Riding it would leave someone invisible in the
    // pending list until an unrelated reconnect happened to fire.
    const { $changeEventsAvailable, $pairingChangeTick, $platformsChangeTick } = await import('@/store/live-sync')

    getMessagingPlatforms.mockResolvedValue({ platforms: [platform()] })
    getPairing.mockResolvedValue({ approved: [], pending: [] })

    await renderMessaging()
    await act(async () => {
      $changeEventsAvailable.set(true)
    })
    getPairing.mockClear()

    // Someone DMs the bot: the store moves, the watcher ticks pairing.changed.
    getPairing.mockResolvedValue({ approved: [], pending: [pendingUser] })
    await act(async () => {
      $pairingChangeTick.set($pairingChangeTick.get() + 1)
    })

    await waitFor(() => expect(getPairing).toHaveBeenCalled())
    expect(await screen.findByRole('button', { name: 'Approve' })).toBeTruthy()

    // A platform health tick alone must not be what fetches pairing.
    getPairing.mockClear()
    await act(async () => {
      $platformsChangeTick.set($platformsChangeTick.get() + 1)
    })
    expect(getPairing).not.toHaveBeenCalled()
  })
})

describe('MessagingView WhatsApp onboarding', () => {
  const whatsapp = platform({
    id: 'whatsapp',
    name: 'WhatsApp',
    whatsapp_setup: { allowed_users_set: false, home_channel_set: false, mode: '' }
  })

  it('starts pairing and renders the backend QR payload locally', async () => {
    getMessagingPlatforms.mockResolvedValue({ platforms: [whatsapp] })
    startWhatsAppOnboarding.mockResolvedValue({
      allowed_users: '',
      expires_at: '2099-01-01T00:00:00Z',
      mode: 'bot',
      pairing_id: 'wa-pairing-1',
      qr_payload: 'backend-qr-payload',
      status: 'waiting'
    })

    await renderMessaging()
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Pair with QR' }))
    })

    await waitFor(() =>
      expect(startWhatsAppOnboarding).toHaveBeenCalledWith({ allowed_users: '', mode: 'bot' }, 'default')
    )
    expect(await screen.findByRole('img', { name: 'WhatsApp setup QR code' })).toBeTruthy()
    expect(screen.getByText('Scan with WhatsApp Linked Devices, not the camera app.')).toBeTruthy()
  })

  it('stops polling and offers a retry when the WhatsApp QR expires', async () => {
    getMessagingPlatforms.mockResolvedValue({ platforms: [whatsapp] })
    startWhatsAppOnboarding.mockResolvedValue({
      allowed_users: '',
      expires_at: '2099-01-01T00:00:00Z',
      mode: 'bot',
      pairing_id: 'wa-expired-pairing',
      qr_payload: 'backend-qr-payload',
      status: 'waiting'
    })
    getWhatsAppOnboardingStatus.mockRejectedValue(new Error('410 Gone: WhatsApp setup expired.'))

    await renderMessaging()
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Pair with QR' }))
    })

    expect(await screen.findByText('WhatsApp QR setup expired. Start a new QR setup to try again.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Pair with QR' })).toBeTruthy()
    expect(getWhatsAppOnboardingStatus).toHaveBeenCalledTimes(1)
  })

  it('clears a pairing superseded by another WhatsApp setup', async () => {
    getMessagingPlatforms.mockResolvedValue({ platforms: [whatsapp] })
    startWhatsAppOnboarding.mockResolvedValue({
      allowed_users: '',
      expires_at: '2099-01-01T00:00:00Z',
      mode: 'bot',
      pairing_id: 'wa-superseded-pairing',
      qr_payload: 'stale-qr-payload',
      status: 'waiting'
    })
    getWhatsAppOnboardingStatus.mockResolvedValue({
      allowed_users: '',
      error: 'WhatsApp setup was replaced by a newer pairing session.',
      expires_at: '2099-01-01T00:00:00Z',
      mode: 'bot',
      pairing_id: 'wa-superseded-pairing',
      qr_payload: 'stale-qr-payload',
      status: 'cancelled'
    })

    await renderMessaging()
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Pair with QR' }))
    })

    expect(await screen.findByText('WhatsApp setup was replaced by a newer pairing session.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Pair with QR' })).toBeTruthy()
    expect(screen.queryByRole('img', { name: 'WhatsApp setup QR code' })).toBeNull()
  })

  it('applies an already-linked WhatsApp session and refreshes backend truth', async () => {
    getMessagingPlatforms.mockResolvedValue({ platforms: [whatsapp] })
    startWhatsAppOnboarding.mockResolvedValue({
      account_id: '15551234567:1@s.whatsapp.net',
      account_phone: '15551234567',
      allowed_users: '',
      expires_at: '2099-01-01T00:00:00Z',
      mode: 'bot',
      pairing_id: 'wa-pairing-2',
      qr_payload: null,
      status: 'connected'
    })
    applyWhatsAppOnboarding.mockResolvedValue({ ok: true, platform: 'whatsapp', restart_started: true })

    await renderMessaging()
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Pair with QR' }))
    })
    getMessagingPlatforms.mockClear()
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Save and restart' }))
    })

    await waitFor(() =>
      expect(applyWhatsAppOnboarding).toHaveBeenCalledWith(
        'wa-pairing-2',
        { allowed_users: '', mode: 'bot' },
        'default'
      )
    )
    expect(getMessagingPlatforms).toHaveBeenCalledTimes(1)
  })

  it('cancels pairing on its original profile when the active profile changes', async () => {
    const { $activeGatewayProfile } = await import('@/store/profile')
    getMessagingPlatforms.mockResolvedValue({ platforms: [whatsapp] })
    startWhatsAppOnboarding.mockResolvedValue({
      allowed_users: '',
      expires_at: '2099-01-01T00:00:00Z',
      mode: 'bot',
      pairing_id: 'wa-profile-pairing',
      qr_payload: 'backend-qr-payload',
      status: 'waiting'
    })

    await renderMessaging()
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Pair with QR' }))
    })
    await waitFor(() =>
      expect(startWhatsAppOnboarding).toHaveBeenCalledWith({ allowed_users: '', mode: 'bot' }, 'default')
    )

    await act(async () => {
      $activeGatewayProfile.set('work')
    })

    await waitFor(() => expect(cancelWhatsAppOnboarding).toHaveBeenCalledWith('wa-profile-pairing', 'default'))
    $activeGatewayProfile.set('default')
  })
})
