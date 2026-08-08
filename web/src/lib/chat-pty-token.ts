const PRIMARY_PTY_ATTACH_TOKEN_KEY = "hermes.pty.token.chat";
const CHAT_PTY_ATTACH_TOKEN_PREFIX = `${PRIMARY_PTY_ATTACH_TOKEN_KEY}.`;

export function chatPtyTokenStorageKey(tabId: string): string {
  return tabId === "primary"
    ? PRIMARY_PTY_ATTACH_TOKEN_KEY
    : `${CHAT_PTY_ATTACH_TOKEN_PREFIX}${tabId}`;
}

export function readChatPtyToken(tabId: string): string | null {
  try {
    return window.localStorage.getItem(chatPtyTokenStorageKey(tabId));
  } catch {
    return null;
  }
}

export function clearChatPtyToken(tabId: string): void {
  try {
    window.localStorage.removeItem(chatPtyTokenStorageKey(tabId));
  } catch {
    // Private mode / blocked storage: the server's idle reaper is the fallback.
  }
}

export function getOrCreateChatPtyToken(
  tabId: string,
  rotate = false,
): string {
  let token = rotate ? "" : readChatPtyToken(tabId) ?? "";
  if (!token) {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    token = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
      "",
    );
    try {
      window.localStorage.setItem(chatPtyTokenStorageKey(tabId), token);
    } catch {
      // Private mode / blocked storage: token remains valid for this mount.
    }
  }
  return token;
}
