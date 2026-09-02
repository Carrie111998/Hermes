import { shouldBlockPtyInput, type PtyConnectionState } from "./pty-reconnect";

const WS_OPEN = 1;

export type PtyKeyboardShortcut =
  | "browser-address-bar"
  | "copy"
  | "delete-word-backward"
  | "delete-word-forward"
  | "pass";

export function resolvePtyKeyboardShortcut(
  ev: Pick<KeyboardEvent, "altKey" | "ctrlKey" | "key" | "metaKey" | "shiftKey">,
  isMac: boolean,
  hasTerminalSelection: boolean,
): PtyKeyboardShortcut {
  const key = ev.key.toLowerCase();
  const copyPressed = isMac
    ? ev.metaKey && !ev.ctrlKey
    : ev.ctrlKey && !ev.altKey && !ev.metaKey;

  if (copyPressed && key === "c" && hasTerminalSelection) {
    return "copy";
  }

  // Browsers reserve Cmd/Ctrl+L for the address bar. Some xterm/browser
  // combinations still let the plain printable "l" fall through to onData
  // even though the browser consumes the shortcut, which appends stray
  // characters to the prompt on each refresh/navigation. Swallow it at the
  // terminal boundary so browser chrome shortcuts never type into Hermes.
  if (
    copyPressed &&
    !ev.shiftKey &&
    key === "l"
  ) {
    return "browser-address-bar";
  }

  if (
    ev.ctrlKey &&
    !ev.shiftKey &&
    !ev.altKey &&
    !ev.metaKey &&
    ev.key === "Backspace"
  ) {
    return "delete-word-backward";
  }

  if (
    ev.ctrlKey &&
    !ev.shiftKey &&
    !ev.altKey &&
    !ev.metaKey &&
    ev.key === "Delete"
  ) {
    return "delete-word-forward";
  }

  return "pass";
}

// Guarded sender for shortcut escape sequences. Applies the same gate as the
// term.onData path (socket OPEN + shouldBlockPtyInput) so a reconnecting or
// closed session never receives shortcut bytes.
export function sendPtyShortcutSequence(
  ws: Pick<WebSocket, "readyState" | "send"> | null,
  ptyState: PtyConnectionState,
  sequence: string,
): boolean {
  if (!ws || ws.readyState !== WS_OPEN || shouldBlockPtyInput(ptyState)) {
    return false;
  }

  ws.send(sequence);

  return true;
}
