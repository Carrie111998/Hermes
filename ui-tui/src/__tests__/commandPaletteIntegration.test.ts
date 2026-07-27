import { beforeEach, describe, expect, it } from 'vitest'

import { ACTION_REGISTRY } from '../app/actionRegistry.js'
import { getOverlayState, patchOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { coreCommands } from '../app/slash/commands/core.js'
import { actionIdForSlash, findSlashCommand } from '../app/slash/registry.js'
import { handleCommandPaletteHotkey } from '../app/useInputHandlers.js'
import { shouldRenderCompletions } from '../components/appOverlays.js'

describe('command palette integration', () => {
  beforeEach(() => resetOverlayState())

  it('opens Ctrl+P as the sole floating navigation overlay', () => {
    patchOverlayState({ sessions: true })

    expect(handleCommandPaletteHotkey('p', { ctrl: true })).toBe(true)
    expect(getOverlayState()).toEqual(
      expect.objectContaining({ commandPalette: { query: '' }, modelPicker: false, sessions: false })
    )
  })

  it('suppresses legacy composer completions while the action palette is open', () => {
    expect(shouldRenderCompletions(null, 2)).toBe(true)
    expect(shouldRenderCompletions({ query: '' }, 2)).toBe(false)
  })

  it('keeps /help compatible while mapped slash commands retain action parity', () => {
    expect(findSlashCommand('help')).toBe(coreCommands.find(command => command.name === 'help'))
    expect(getOverlayState().commandPalette).toBeNull()
    expect(actionIdForSlash('sessions')).toBe('session.switch')
    expect(actionIdForSlash('resume')).toBe('session.switch')
    expect(ACTION_REGISTRY.get(actionIdForSlash('model')!)?.title).toBe('Switch model')
    expect(findSlashCommand('logs')).toBeDefined()
  })
})
