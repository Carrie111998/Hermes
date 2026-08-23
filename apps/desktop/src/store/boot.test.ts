import { describe, expect, it } from 'vitest'

import {
  $desktopBoot,
  applyDesktopBootProgress,
  completeDesktopBoot,
  failDesktopBoot,
  resumeDesktopBootForRetry
} from './boot'

// #92927: the dead-IPC-bridge boot failure must be distinguishable from a
// generic boot failure so the recovery overlay can stop offering
// bridge-dependent dead ends (Repair / Settings / Open logs all round-trip
// through window.hermesDesktop) and instead show the actionable repair copy.

describe('boot store errorKind (#92927)', () => {
  it('failDesktopBoot records the failure kind', () => {
    failDesktopBoot('Desktop IPC bridge is unavailable.', 'ipc-bridge')

    expect($desktopBoot.get().error).toBe('Desktop IPC bridge is unavailable.')
    expect($desktopBoot.get().errorKind).toBe('ipc-bridge')
  })

  it('failDesktopBoot without a kind leaves errorKind undefined', () => {
    failDesktopBoot('Hermes backend exited before it became ready.')

    expect($desktopBoot.get().errorKind).toBeUndefined()
  })

  it('a later boot-progress event cannot clobber the latched failure kind', () => {
    failDesktopBoot('Desktop IPC bridge is unavailable.', 'ipc-bridge')

    applyDesktopBootProgress({
      error: null,
      fakeMode: false,
      message: 'backend.ready',
      phase: 'backend.ready',
      progress: 94,
      running: true,
      timestamp: Date.now()
    })

    expect($desktopBoot.get().error).toBe('Desktop IPC bridge is unavailable.')
    expect($desktopBoot.get().errorKind).toBe('ipc-bridge')
  })

  it('completeDesktopBoot clears the failure kind', () => {
    failDesktopBoot('Desktop IPC bridge is unavailable.', 'ipc-bridge')
    completeDesktopBoot()

    expect($desktopBoot.get().error).toBeNull()
    expect($desktopBoot.get().errorKind).toBeUndefined()
  })

  it('resumeDesktopBootForRetry clears the failure kind while retrying', () => {
    failDesktopBoot('Desktop IPC bridge is unavailable.', 'ipc-bridge')
    resumeDesktopBootForRetry('retrying…')

    expect($desktopBoot.get().error).toBeNull()
    expect($desktopBoot.get().errorKind).toBeUndefined()
  })
})
