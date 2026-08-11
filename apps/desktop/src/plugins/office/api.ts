/** Hermes Office (Claw3d) — renderer-side API over the Electron claw3d bridge. */
import { atom } from '@hermes/plugin-sdk'

export interface OfficeStatus {
  cloned: boolean
  installed: boolean
  devServerRunning: boolean
  adapterRunning: boolean
  running: boolean
  port: number
  portInUse: boolean
  url: string
  error: string
  oauthUnsupported: boolean
}

export interface SetupProgress {
  step: number
  totalSteps: number
  title: string
  detail: string
  log: string
}

/** Setup progress ('' = not running). */
export const $setupProgress = atom<SetupProgress | null>(null)

function bridge() {
  return window.hermesDesktop.claw3d
}

export function bindOfficeApi(): () => void {
  const off = bridge().onSetupProgress(progress => $setupProgress.set(progress))

  return () => {
    off()
    $setupProgress.set(null)
  }
}

export const getStatus = (profile?: string | null) => bridge().getStatus(profile)
export const runSetup = (profile?: string | null) => bridge().setup(profile)
export const startOffice = (profile?: string | null) => bridge().start(profile)
export const stopOffice = () => bridge().stop()
export const getLogs = () => bridge().getLogs()
export const openOffice = (profile?: string | null) => bridge().open(profile)
