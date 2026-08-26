import { describe, expect, it, vi } from "vitest";

import { connectNativeVoiceBridge } from "./native-voice-bridge";

describe("origin-scoped native voice bridge", () => {
  it("sends only the declared command envelope and accepts declared transcript events", () => {
    let listener: ((event: MessageEvent) => void) | undefined;
    const target = {
      postMessage: vi.fn(),
      addEventListener: vi.fn((_name: string, cb: (event: MessageEvent) => void) => { listener = cb; }),
      removeEventListener: vi.fn(),
    };
    const events = vi.fn();
    const bridge = connectNativeVoiceBridge(target, events);
    expect(bridge).not.toBeNull();
    bridge?.command("check");
    bridge?.command("start");
    expect(target.postMessage).toHaveBeenNthCalledWith(1, JSON.stringify({ version: 1, command: "check" }));
    expect(target.postMessage).toHaveBeenNthCalledWith(2, JSON.stringify({ version: 1, command: "start" }));

    listener?.({ data: JSON.stringify({ version: 1, event: "partial", transcript: "hello" }) } as MessageEvent);
    listener?.({ data: JSON.stringify({ version: 1, event: "unknown", transcript: "ignored" }) } as MessageEvent);
    expect(events).toHaveBeenCalledOnce();
    expect(events).toHaveBeenCalledWith({ version: 1, event: "partial", transcript: "hello" });
  });

  it("fails closed for missing or malformed bridge objects and events", () => {
    expect(connectNativeVoiceBridge(undefined, vi.fn())).toBeNull();
    const events = vi.fn();
    let listener: ((event: MessageEvent) => void) | undefined;
    const target = {
      postMessage: vi.fn(),
      addEventListener: vi.fn((_name: string, cb: (event: MessageEvent) => void) => { listener = cb; }),
      removeEventListener: vi.fn(),
    };
    connectNativeVoiceBridge(target, events);
    listener?.({ data: "not json" } as MessageEvent);
    listener?.({ data: JSON.stringify({ version: 2, event: "final", transcript: "no" }) } as MessageEvent);
    expect(events).not.toHaveBeenCalled();
  });
});
