/**
 * Terminal key mapping for the embedded TUI (xterm.js).
 *
 * xterm.js collapses Shift+Enter into a bare `\r` — it never emits the
 * modified-key sequence the Hermes TUI expects for a multiline line break.
 * The dashboard therefore translates the DOM keydown into the sequence the
 * TUI already understands (see ui-tui/packages/hermes-ink/src/ink/parse-keypress.ts:
 * `ESC[13;2u` = Shift+Enter, and ui-tui/src/components/textInput.tsx, where
 * `k.return && k.shift` inserts `\n` instead of submitting).
 */
export const SHIFT_ENTER_SEQUENCE = "\x1b[13;2u";

/** Minimal keyboard-event shape the mapping reads. */
export interface ShiftEnterCandidate {
  key: string;
  shiftKey: boolean;
}

/**
 * Returns the terminal sequence to feed the PTY when the keydown is
 * Shift+Enter, or `null` for any other key. Pure, so it is unit-testable
 * without a DOM.
 */
export function shiftEnterSequence(
  ev: ShiftEnterCandidate,
): string | null {
  if (ev.key === "Enter" && ev.shiftKey) {
    return SHIFT_ENTER_SEQUENCE;
  }
  return null;
}
