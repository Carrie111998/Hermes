import { beforeEach, describe, expect, it } from 'vitest'

import {
  $backendThemes,
  $pendingSkinApplies,
  __resetBackendSkinSync,
  activateBackendSkinProfile,
  ingestBackendSkin,
  ingestGatewayReadySkin
} from './backend-sync'
import { PROFILE_SKINS_STORAGE_KEY, SKIN_STORAGE_KEY } from './skin-preference'

const skin = (name: string) => ({
  name,
  colors: { background: '#101020', ui_accent: '#ff33aa', banner_text: '#eeeeee' }
})

const profileSkin = (name: string, foreground: string) => ({
  name,
  colors: { background: '#101020', ui_accent: '#ff33aa', ui_text: foreground }
})

describe('ingestBackendSkin', () => {
  beforeEach(() => {
    window.localStorage.clear()
    __resetBackendSkinSync()
  })

  it('registers a converted skin without applying when apply=false', () => {
    ingestBackendSkin(skin('neon'), { apply: false })

    expect($backendThemes.get().neon?.name).toBe('neon')
    expect($pendingSkinApplies.get()).toEqual([])
  })

  it('applies a new skin name once', () => {
    ingestBackendSkin(skin('neon'), { apply: true })

    expect($pendingSkinApplies.get()).toEqual([{ name: 'neon', profile: 'default' }])
  })

  it('does not re-apply the same skin name', () => {
    ingestBackendSkin(skin('neon'), { apply: true })
    $pendingSkinApplies.set([])
    ingestBackendSkin(skin('neon'), { apply: true })

    expect($pendingSkinApplies.get()).toEqual([])
  })

  it('applies again when the skin name changes', () => {
    ingestBackendSkin(skin('neon'), { apply: true })
    $pendingSkinApplies.set([])
    ingestBackendSkin(skin('forest'), { apply: true })

    expect($pendingSkinApplies.get()).toEqual([{ name: 'forest', profile: 'default' }])
  })

  it('seed does not paint, but a later same-name skin.changed applies (missed-activation recovery)', () => {
    // Connect while display.skin is already neon: seed records the baseline
    // without painting (never stomp the persisted desktop theme on connect).
    ingestBackendSkin(skin('neon'), { apply: false }) // gateway.ready seed
    expect($pendingSkinApplies.get()).toEqual([])

    // The activation event was missed (skin set while disconnected / backend
    // restarted). Hermes re-affirms it — `hermes config set display.skin neon`
    // or a `hermes skin set` recolor. That explicit event must repaint even
    // though the name matches the seed.
    ingestBackendSkin(skin('neon'), { apply: true })
    expect($pendingSkinApplies.get()).toEqual([{ name: 'neon', profile: 'default' }])

    // Once applied, a repeat same-name event is a no-op again...
    $pendingSkinApplies.set([])
    ingestBackendSkin(skin('neon'), { apply: true })
    expect($pendingSkinApplies.get()).toEqual([])

    // ...and a genuine switch still applies.
    ingestBackendSkin(skin('forest'), { apply: true }) // Hermes authored a new skin
    expect($pendingSkinApplies.get()).toEqual([{ name: 'forest', profile: 'default' }])
  })

  it('a reconnect re-seed after a real apply does not downgrade the applied baseline', () => {
    ingestBackendSkin(skin('neon'), { apply: true }) // applied for real
    $pendingSkinApplies.set([])

    ingestBackendSkin(skin('neon'), { apply: false }) // reconnect: gateway.ready re-seed
    ingestBackendSkin(skin('neon'), { apply: true }) // repeat event (e.g. in-place recolor)

    // Already painted once — the repeat must not re-apply (protects a manual
    // desktop-side theme switch from being snapped back after a reconnect).
    expect($pendingSkinApplies.get()).toEqual([])
  })

  it('never registers default in the backend store (desktop keeps its own palette)', () => {
    ingestBackendSkin(skin('default'), { apply: true })

    expect($backendThemes.get().default).toBeUndefined()
  })

  it('does not apply default on the connect-time seed', () => {
    ingestBackendSkin(skin('default'), { apply: false })

    expect($pendingSkinApplies.get()).toEqual([])
  })

  it('applies a runtime switch back to default (repaints the desktop to its own default)', () => {
    ingestBackendSkin(skin('neon'), { apply: false }) // gateway.ready seed on some skin
    ingestBackendSkin(skin('default'), { apply: true }) // Hermes switched back to default

    expect($pendingSkinApplies.get()).toEqual([{ name: 'default', profile: 'default' }])
  })

  it('does not shadow a built-in name but can still apply it', () => {
    ingestBackendSkin(skin('mono'), { apply: true })

    expect($backendThemes.get().mono).toBeUndefined()
    expect($pendingSkinApplies.get()).toEqual([{ name: 'mono', profile: 'default' }])
  })

  it('ignores empty payloads', () => {
    ingestBackendSkin(undefined, { apply: true })
    ingestBackendSkin({ name: '' }, { apply: true })

    expect($pendingSkinApplies.get()).toEqual([])
  })

  it('isolates same-name palettes and apply baselines between profiles', () => {
    activateBackendSkinProfile('work')
    ingestBackendSkin(profileSkin('shared', '#aaaaaa'), { apply: true, profile: 'work' })

    expect($backendThemes.get().shared?.colors.foreground).toBe('#aaaaaa')

    $pendingSkinApplies.set([])
    ingestBackendSkin(profileSkin('shared', '#bbbbbb'), { apply: false, profile: 'personal' })

    // A background profile cannot replace the foreground profile's palette or
    // downgrade its "already applied" baseline.
    expect($backendThemes.get().shared?.colors.foreground).toBe('#aaaaaa')
    ingestBackendSkin(profileSkin('shared', '#aaaaaa'), { apply: true, profile: 'work' })
    expect($pendingSkinApplies.get()).toEqual([])

    activateBackendSkinProfile('personal')
    expect($backendThemes.get().shared?.colors.foreground).toBe('#bbbbbb')

    activateBackendSkinProfile('work')
    expect($backendThemes.get().shared?.colors.foreground).toBe('#aaaaaa')
  })

  it('coalesces rapid applies per profile without dropping other profiles', () => {
    ingestBackendSkin(skin('neon'), { apply: true, profile: 'work' })
    ingestBackendSkin(skin('forest'), { apply: true, profile: 'personal' })
    ingestBackendSkin(skin('mono'), { apply: true, profile: 'work' })

    expect($pendingSkinApplies.get()).toEqual([
      { name: 'forest', profile: 'personal' },
      { name: 'mono', profile: 'work' }
    ])
  })
})

describe('ingestGatewayReadySkin', () => {
  beforeEach(() => {
    window.localStorage.clear()
    __resetBackendSkinSync()
  })

  it('adopts display.skin when Desktop has no persisted choice', () => {
    ingestGatewayReadySkin(skin('neon'), 'default')

    expect($backendThemes.get().neon?.name).toBe('neon')
    expect($pendingSkinApplies.get()).toEqual([{ name: 'neon', profile: 'default' }])
  })

  it('keeps backend default as no palette opinion', () => {
    ingestGatewayReadySkin(skin('default'), 'default')

    expect($pendingSkinApplies.get()).toEqual([])
  })

  it('preserves a persisted default-profile choice', () => {
    window.localStorage.setItem(SKIN_STORAGE_KEY, 'mono')

    ingestGatewayReadySkin(skin('neon'), 'default')

    expect($backendThemes.get().neon?.name).toBe('neon')
    expect($pendingSkinApplies.get()).toEqual([])
  })

  it('preserves a named profile choice', () => {
    window.localStorage.setItem(PROFILE_SKINS_STORAGE_KEY, JSON.stringify({ work: 'mono' }))

    ingestGatewayReadySkin(skin('neon'), 'work')

    expect($pendingSkinApplies.get()).toEqual([])
  })

  it('preserves the global choice inherited by an unassigned named profile', () => {
    window.localStorage.setItem(SKIN_STORAGE_KEY, 'slate')

    ingestGatewayReadySkin(skin('neon'), 'work')

    expect($pendingSkinApplies.get()).toEqual([])
  })

  it('does not let another profile assignment block first-use adoption', () => {
    window.localStorage.setItem(PROFILE_SKINS_STORAGE_KEY, JSON.stringify({ personal: 'mono' }))

    ingestGatewayReadySkin(skin('neon'), 'work')

    expect($pendingSkinApplies.get()).toEqual([{ name: 'neon', profile: 'work' }])
  })
})
