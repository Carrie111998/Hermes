import { describe, expect, it, vi } from "vitest";

import { submitVoiceTranscriptToPty } from "./pty-voice-submit";

describe("submitVoiceTranscriptToPty", () => {
  it("sends transcript then exactly one Return in a separate PTY frame", () => {
    vi.useFakeTimers();
    const socket = { readyState: WebSocket.OPEN, send: vi.fn() };
    const current = () => socket;

    expect(submitVoiceTranscriptToPty(current, "Call the crew")).toBe(true);
    expect(socket.send).toHaveBeenCalledTimes(1);
    expect(socket.send).toHaveBeenNthCalledWith(1, "Call the crew");
    vi.advanceTimersByTime(100);
    expect(socket.send).toHaveBeenCalledTimes(2);
    expect(socket.send).toHaveBeenNthCalledWith(2, "\r");
    vi.useRealTimers();
  });

  it("does not send Return through a replacement or closed socket", () => {
    vi.useFakeTimers();
    const first = { readyState: WebSocket.OPEN, send: vi.fn<(data: string) => void>() };
    const second = { readyState: WebSocket.OPEN, send: vi.fn<(data: string) => void>() };
    let active: { readyState: number; send(data: string): void } = first;
    expect(submitVoiceTranscriptToPty(() => active, "Hello")).toBe(true);
    active = second;
    vi.advanceTimersByTime(100);
    expect(first.send).toHaveBeenCalledOnce();
    expect(second.send).not.toHaveBeenCalled();

    active = { readyState: WebSocket.CLOSED, send: vi.fn<(data: string) => void>() };
    expect(submitVoiceTranscriptToPty(() => active, "Nope")).toBe(false);
    vi.useRealTimers();
  });
});
