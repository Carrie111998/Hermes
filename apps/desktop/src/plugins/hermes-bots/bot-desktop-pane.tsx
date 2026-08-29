import { BotDesktopSurface, host, translateNow } from '@hermes/plugin-sdk'

import { ID } from './shared'

const openPanes = new Map<string, () => void>()

/** Open the Bot's desktop inside Hermes' normal main-area workspace. */
export function openBotDesktopPane(profile: string) {
  const key = String(profile || '').trim() || 'default'
  const existing = openPanes.get(key)

  if (existing) {
    return
  }

  if (typeof host.openWorkspace !== 'function') {
    throw new Error('This Hermes Desktop version does not support embedded workspaces.')
  }

  const close = host.openWorkspace(`${ID}:desktop:${key}`, {
    minWidth: '30rem',
    onClose: () => {
      if (openPanes.get(key) === close) {
        openPanes.delete(key)
      }
    },
    render: () => <BotDesktopSurface profile={key} />,
    title: translateNow('desktop.botDesktop.profile', key)
  })

  openPanes.set(key, close)
}

export function closeBotDesktopPane(profile: string) {
  const key = String(profile || '').trim() || 'default'
  const close = openPanes.get(key)

  if (!close) {
    return false
  }

  openPanes.delete(key)
  close()

  return true
}
