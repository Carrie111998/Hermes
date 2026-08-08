/**
 * Strip terminal control-response / mouse-report fragments from text that
 * reached the composer.
 *
 * When you return to a terminal and move the mouse or scroll, the terminal
 * emits SGR mouse reports (`ESC[<b;x;yM`), and mode/focus/DA queries can be
 * answered with DECRPM (`ESC[?mode;state$y`) and DA1 (`ESC[?params c`)
 * responses. The hermes-ink input parser routes complete sequences away from
 * the keypress path (responses to the querier, mouse events to mouse
 * handlers), and the CLI strips leaked fragments in both its paste and
 * prompt-buffer paths. This is the TUI composer's belt-and-suspenders
 * boundary guard: if a fragment still arrives — a torn sequence, an older
 * gateway, a future parser gap — it is control traffic, never user intent,
 * so it is dropped instead of typed.
 *
 * Covered forms:
 *  - SGR mouse reports: `ESC[<b;x;yM/m`, visible `^[[<b;x;yM`, bare `<b;x;yM`
 *  - bare mouse-report runs: >=3 consecutive `b;x;yM` payload fragments (the
 *    shape that floods the input when the `ESC[<` prefix is consumed first)
 *  - CPR/DSR responses: `ESC[row;colR` and visible form
 *  - DECRPM responses: `ESC[?mode;state$y`, visible form, and bare `mode;state$y`
 */

// eslint-disable-next-line no-control-regex
const SGR_MOUSE_ESC = /\x1b\[<\d+;\d+;\d+[Mm]/g
const SGR_MOUSE_VISIBLE = /\^\[\[<\d+;\d+;\d+[Mm]/g
const SGR_MOUSE_BARE = /<\d+;\d+;\d+[Mm]/g
// eslint-disable-next-line no-control-regex
const CPR_ESC = /\x1b\[\d+;\d+R/g
const CPR_VISIBLE = /\^\[\[\d+;\d+R/g
// eslint-disable-next-line no-control-regex
const DECRPM_ESC = /\x1b\[\?\d+;\d+\$y/g
const DECRPM_VISIBLE = /\^\[\[\?\d+;\d+\$y/g
const DECRPM_BARE = /\d+;\d+\$y/g
const BARE_MOUSE_RUN = /(?:\d+;\d+;\d+[Mm]){3,}/g

export function stripTerminalControlFragments(text: string): string {
  if (!text) {
    return text
  }

  return text
    .replace(SGR_MOUSE_ESC, '')
    .replace(SGR_MOUSE_VISIBLE, '')
    .replace(CPR_ESC, '')
    .replace(CPR_VISIBLE, '')
    .replace(DECRPM_ESC, '')
    .replace(DECRPM_VISIBLE, '')
    .replace(DECRPM_BARE, '')
    .replace(SGR_MOUSE_BARE, '')
    .replace(BARE_MOUSE_RUN, '')
}
