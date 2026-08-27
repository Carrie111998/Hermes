import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $profileSwitchBehavior,
  $profileSwitchRestoreToken,
  _resetProfileSwitchBehaviorForTests,
  bindProfileSwitchRestore,
  clearProfileSwitchRestore,
  getProfileSwitchBehavior,
  isCurrentProfileSwitchGeneration,
  observeProfileSwitchRestoreNavigation,
  registerProfileSwitchAnchorCapture,
  requestProfileSwitchRestore,
  setProfileSwitchBehavior,
  supersedeProfileSwitchRestoreForFreshDraft
} from './profile-switch-behavior'

describe('profile switch behavior', () => {
  const sourcePaneFocus = { groupId: 'source-zone', paneId: 'session-tile:source-tab' }
  const workspacePaneFocus = { groupId: null, paneId: 'workspace' }

  beforeEach(() => {
    localStorage.clear()
    _resetProfileSwitchBehaviorForTests()
  })

  afterEach(() => {
    localStorage.clear()
    _resetProfileSwitchBehaviorForTests()
  })

  it('defaults to and persists the fresh-draft preference', () => {
    expect(getProfileSwitchBehavior()).toBe('fresh_draft')

    setProfileSwitchBehavior('restore_last_session')

    expect($profileSwitchBehavior.get()).toBe('restore_last_session')
    expect(localStorage.getItem('hermes.desktop.profile-switch-behavior.v1')).toBe('restore_last_session')
  })

  it('allows the native source -> null selection -> fresh-route publication, then rejects a newer request', () => {
    const capture = vi.fn(() => ({
      focusedStoredSessionId: 'source-tab',
      paneFocus: sourcePaneFocus,
      pathname: '/session/source'
    }))
    const unregister = registerProfileSwitchAnchorCapture(capture)
    const token = requestProfileSwitchRestore('beta', null, 7)

    expect(capture).toHaveBeenCalledOnce()
    expect(
      observeProfileSwitchRestoreNavigation(
        token.generation,
        { focusedStoredSessionId: 'source-tab', paneFocus: sourcePaneFocus, pathname: '/session/source' },
        7
      )
    ).toBe(false)
    expect($profileSwitchRestoreToken.get()?.generation).toBe(token.generation)

    expect(
      observeProfileSwitchRestoreNavigation(
        token.generation,
        { focusedStoredSessionId: null, paneFocus: workspacePaneFocus, pathname: '/session/source' },
        7
      )
    ).toBe(false)
    expect($profileSwitchRestoreToken.get()?.generation).toBe(token.generation)

    expect(
      observeProfileSwitchRestoreNavigation(
        token.generation,
        { focusedStoredSessionId: null, paneFocus: workspacePaneFocus, pathname: '/' },
        7
      )
    ).toBe(true)
    expect($profileSwitchRestoreToken.get()?.generation).toBe(token.generation)
    expect(
      observeProfileSwitchRestoreNavigation(
        token.generation,
        { focusedStoredSessionId: null, paneFocus: workspacePaneFocus, pathname: '/' },
        7
      )
    ).toBe(true)

    expect(
      observeProfileSwitchRestoreNavigation(
        token.generation,
        { focusedStoredSessionId: null, paneFocus: workspacePaneFocus, pathname: '/' },
        8
      )
    ).toBe(false)
    expect($profileSwitchRestoreToken.get()).toBeNull()
    unregister()
  })

  it('cancels when the initial fresh route is focused outside the workspace', () => {
    const unregister = registerProfileSwitchAnchorCapture(() => ({
      focusedStoredSessionId: 'source-tab',
      paneFocus: sourcePaneFocus,
      pathname: '/session/source'
    }))
    const token = requestProfileSwitchRestore('beta', null, 7)

    expect(
      observeProfileSwitchRestoreNavigation(
        token.generation,
        { focusedStoredSessionId: null, paneFocus: { groupId: 'native-zone', paneId: 'terminal' }, pathname: '/' },
        7
      )
    ).toBe(false)
    expect($profileSwitchRestoreToken.get()).toBeNull()
    unregister()
  })

  it('cancels when a non-session pane takes focus during the native intermediate', () => {
    const unregister = registerProfileSwitchAnchorCapture(() => ({
      focusedStoredSessionId: 'source-tab',
      paneFocus: sourcePaneFocus,
      pathname: '/session/source'
    }))
    const token = requestProfileSwitchRestore('beta', null, 7)

    expect(
      observeProfileSwitchRestoreNavigation(
        token.generation,
        {
          focusedStoredSessionId: null,
          paneFocus: { groupId: 'native-zone', paneId: 'terminal' },
          pathname: '/session/source'
        },
        7
      )
    ).toBe(false)
    expect($profileSwitchRestoreToken.get()).toBeNull()
    unregister()
  })

  it.each([
    ['named local profile', null, 'local', 'beta'],
    ['dedicated remote override', null, 'remote-override', 'beta'],
    ['shared-primary descriptor', null, 'shared-primary', 'beta'],
    ['legacy profile without a descriptor id', null, null, null],
    ['published shared-primary route', 'shared-primary', 'shared-primary', 'beta']
  ] as const)(
    'binds a profile-only %s without conflating live and descriptor identity',
    (_label, live, descriptor, descriptorProfile) => {
      const token = requestProfileSwitchRestore('beta', null, 1)

      expect(
        bindProfileSwitchRestore(token.generation, {
          activationEpoch: 4,
          descriptorConnectionId: descriptor,
          descriptorProfile,
          liveGatewayConnectionId: live,
          profile: 'beta'
        })
      ).toBe(true)
      expect($profileSwitchRestoreToken.get()).toMatchObject({
        activation: {
          activationEpoch: 4,
          descriptorConnectionId: descriptor,
          descriptorProfile,
          liveGatewayConnectionId: live,
          profile: 'beta'
        }
      })
    }
  )

  it('requires an explicit registry source in both live and committed domains', () => {
    const liveMismatch = requestProfileSwitchRestore('default', 'source-a', 1)

    expect(
      bindProfileSwitchRestore(liveMismatch.generation, {
        activationEpoch: 4,
        descriptorConnectionId: 'source-a',
        descriptorProfile: 'default',
        liveGatewayConnectionId: 'source-b',
        profile: 'default'
      })
    ).toBe(false)

    const descriptorMismatch = requestProfileSwitchRestore('default', 'source-a', 2)

    expect(
      bindProfileSwitchRestore(descriptorMismatch.generation, {
        activationEpoch: 5,
        descriptorConnectionId: 'source-b',
        descriptorProfile: 'default',
        liveGatewayConnectionId: 'source-a',
        profile: 'default'
      })
    ).toBe(false)
    expect($profileSwitchRestoreToken.get()).toBeNull()
  })

  it('allows only the latest generation to bind or clear', () => {
    const toB = requestProfileSwitchRestore('beta', 'source-a', 1)
    const toC = requestProfileSwitchRestore('gamma', 'source-a', 2)

    expect(
      bindProfileSwitchRestore(toB.generation, {
        activationEpoch: 10,
        descriptorConnectionId: 'source-a',
        descriptorProfile: 'beta',
        liveGatewayConnectionId: 'source-a',
        profile: 'beta'
      })
    ).toBe(false)
    clearProfileSwitchRestore(toB.generation)
    expect($profileSwitchRestoreToken.get()?.generation).toBe(toC.generation)

    expect(
      bindProfileSwitchRestore(toC.generation, {
        activationEpoch: 11,
        descriptorConnectionId: 'source-a',
        descriptorProfile: 'gamma',
        liveGatewayConnectionId: 'source-a',
        profile: 'gamma'
      })
    ).toBe(true)
  })

  it('makes a fresh-draft switch supersede pending restore work', () => {
    const restore = requestProfileSwitchRestore('beta', null, 2)

    supersedeProfileSwitchRestoreForFreshDraft(1)
    expect($profileSwitchRestoreToken.get()?.generation).toBe(restore.generation)

    supersedeProfileSwitchRestoreForFreshDraft(3)

    expect(isCurrentProfileSwitchGeneration(restore.generation)).toBe(false)
    expect($profileSwitchRestoreToken.get()).toBeNull()
  })
})
