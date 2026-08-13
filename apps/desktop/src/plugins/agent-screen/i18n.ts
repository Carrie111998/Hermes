/**
 * Plugin-scoped i18n for Agent Screen. English is the source of truth
 * (the app has no `de` locale). Resolved against the app locale via
 * ctx.i18n.register — no core en.ts edit.
 */
import { type PluginLocaleBundles, usePluginI18n } from '@hermes/plugin-sdk'

const en = {
  name: 'Agent Screen',
  chipOn: 'Agent Screen: on — click to stop',
  chipOff: 'Agent Screen: off — click to start',
  chipUnsupported: 'Agent Screen requires a local macOS backend',
  live: 'live · :8788',
  starting: 'starting…',
  off: 'off',
  start: 'Start',
  stop: 'Stop',
  streamWait: 'Stream is not up yet…',
  offHint: 'Agent Screen is off. Click Start — the native window opens.',
  unsupported: 'Agent Screen only runs on a local macOS Hermes backend.',
  previewAlt: 'Agent Screen (live)'
}

export const AGENT_SCREEN_LOCALES: PluginLocaleBundles = { en }

export function useAgentScreenText() {
  const t = usePluginI18n('agent-screen')
  return {
    name: t('name'),
    chipOn: t('chipOn'),
    chipOff: t('chipOff'),
    chipUnsupported: t('chipUnsupported'),
    live: t('live'),
    starting: t('starting'),
    off: t('off'),
    start: t('start'),
    stop: t('stop'),
    streamWait: t('streamWait'),
    offHint: t('offHint'),
    unsupported: t('unsupported'),
    previewAlt: t('previewAlt')
  }
}
