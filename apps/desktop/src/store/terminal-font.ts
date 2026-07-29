import { atom } from 'nanostores'

import { persistBoolean, storedBoolean } from '@/lib/storage'

export const TERMINAL_NERD_FONT_STORAGE_KEY = 'hermes.desktop.terminal.nerd-font.v1'

const DEFAULT_TERMINAL_FONT_FAMILY =
  "'JetBrains Mono', 'Cascadia Code', 'SF Mono', Menlo, Consolas, monospace"

const NERD_TERMINAL_FONT_FAMILY =
  "'FiraCode Nerd Font Mono', 'JetBrainsMono Nerd Font Mono', 'CaskaydiaCove Nerd Font Mono', 'Hack Nerd Font Mono', 'MesloLGM Nerd Font Mono', 'Symbols Nerd Font Mono', " +
  DEFAULT_TERMINAL_FONT_FAMILY

/** Use locally installed Nerd Font glyphs in xterm, falling back to the bundled font. */
export const $terminalNerdFontEnabled = atom(storedBoolean(TERMINAL_NERD_FONT_STORAGE_KEY, false))

$terminalNerdFontEnabled.subscribe(enabled => persistBoolean(TERMINAL_NERD_FONT_STORAGE_KEY, enabled))

export function setTerminalNerdFontEnabled(enabled: boolean): void {
  $terminalNerdFontEnabled.set(enabled)
}

export function terminalFontFamily(nerdFontEnabled: boolean): string {
  return nerdFontEnabled ? NERD_TERMINAL_FONT_FAMILY : DEFAULT_TERMINAL_FONT_FAMILY
}
