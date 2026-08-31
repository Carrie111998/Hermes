import { atom } from 'nanostores'

import type { PreviewUblockState } from '../../electron/preview-ublock'

const unavailableState = (enabled: boolean): PreviewUblockState => ({
  enabled,
  available: false,
  dashboardUrl: null,
  extensionId: null,
  rulesetsReady: false,
  version: null
})

function normalizePreviewUblockState(value: Partial<PreviewUblockState> | null | undefined): PreviewUblockState {
  const enabled = value?.enabled === true

  return {
    enabled,
    available: value?.available === true,
    dashboardUrl: typeof value?.dashboardUrl === 'string' ? value.dashboardUrl : null,
    extensionId: typeof value?.extensionId === 'string' ? value.extensionId : null,
    rulesetsReady: value?.rulesetsReady === true,
    version: typeof value?.version === 'string' ? value.version : null
  }
}

export const $previewUblock = atom<PreviewUblockState>(unavailableState(false))

export async function loadPreviewUblock(): Promise<void> {
  if (typeof window === 'undefined' || typeof window.hermesDesktop?.previewUblock?.getState !== 'function') {
    return
  }

  try {
    $previewUblock.set(normalizePreviewUblockState(await window.hermesDesktop.previewUblock.getState()))
  } catch {
    // Keep the last authoritative state if the main process is not ready.
  }
}

export async function setPreviewUblockEnabled(enabled: boolean): Promise<PreviewUblockState> {
  if (typeof window === 'undefined' || typeof window.hermesDesktop?.previewUblock?.setEnabled !== 'function') {
    throw new Error('Preview uBlock control is unavailable')
  }

  const next = normalizePreviewUblockState(await window.hermesDesktop.previewUblock.setEnabled(enabled))
  $previewUblock.set(next)

  return next
}
