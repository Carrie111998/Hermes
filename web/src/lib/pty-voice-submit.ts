const PTY_TRANSCRIPT_RETURN_DELAY_MS = 100;

interface PtySocket {
  readonly readyState: number;
  send(data: string): void;
}

/**
 * Submit one finalized browser transcript to Ink's composer.
 *
 * Return is deliberately a separate frame: a combined mobile WebSocket frame
 * can be classified as a paste and remain in the composer. The captured socket
 * must still be current before Return is sent, so a reconnect cannot submit the
 * transcript into a replacement PTY session.
 */
export function submitVoiceTranscriptToPty(
  currentSocket: () => PtySocket | null,
  transcript: string,
): boolean {
  const socket = currentSocket();
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;

  socket.send(transcript);
  globalThis.setTimeout(() => {
    if (currentSocket() === socket && socket.readyState === WebSocket.OPEN) {
      socket.send("\r");
    }
  }, PTY_TRANSCRIPT_RETURN_DELAY_MS);
  return true;
}
