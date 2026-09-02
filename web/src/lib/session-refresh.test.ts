import { describe, it, expect } from "vitest";
import {
  shouldRefreshExpandedTranscript,
  shouldRefreshSessions,
  sessionListRevisionKey,
} from "./session-refresh";

describe("shouldRefreshSessions", () => {
  it("returns false on the first poll (no baseline yet)", () => {
    expect(shouldRefreshSessions(null, "s2")).toBe(false);
    expect(
      shouldRefreshSessions(null, {
        newestId: "s2",
        messageCount: 3,
        lastActive: 10,
      }),
    ).toBe(false);
  });

  it("returns false when the current response has no sessions", () => {
    expect(shouldRefreshSessions("s1", null)).toBe(false);
    expect(shouldRefreshSessions(null, null)).toBe(false);
  });

  it("returns false when the newest session id is unchanged (legacy id-only)", () => {
    expect(shouldRefreshSessions("s1", "s1")).toBe(false);
  });

  it("returns true when a new session appears at the head of the list", () => {
    expect(shouldRefreshSessions("s1", "s2")).toBe(true);
    expect(
      shouldRefreshSessions(
        { newestId: "s1", messageCount: 2, lastActive: 1 },
        { newestId: "s2", messageCount: 1, lastActive: 2 },
      ),
    ).toBe(true);
  });

  it("returns true when the same newest id gains messages or activity", () => {
    expect(
      shouldRefreshSessions(
        { newestId: "s1", messageCount: 2, lastActive: 100 },
        { newestId: "s1", messageCount: 4, lastActive: 100 },
      ),
    ).toBe(true);
    expect(
      shouldRefreshSessions(
        { newestId: "s1", messageCount: 2, lastActive: 100 },
        { newestId: "s1", messageCount: 2, lastActive: 200 },
      ),
    ).toBe(true);
  });

  it("returns false when the richer revision is unchanged", () => {
    expect(
      shouldRefreshSessions(
        { newestId: "s1", messageCount: 2, lastActive: 100 },
        { newestId: "s1", messageCount: 2, lastActive: 100 },
      ),
    ).toBe(false);
  });
});

describe("sessionListRevisionKey", () => {
  it("encodes id plus optional count/activity", () => {
    expect(sessionListRevisionKey("abc")).toBe("abc");
    expect(
      sessionListRevisionKey({
        newestId: "abc",
        messageCount: 4,
        lastActive: 9,
      }),
    ).toBe("abc\u00004\u00009");
  });
});

describe("shouldRefreshExpandedTranscript", () => {
  it("returns false until a baseline exists", () => {
    expect(shouldRefreshExpandedTranscript(null, null, 2, 10)).toBe(false);
  });

  it("returns true when message_count or last_active advances", () => {
    expect(shouldRefreshExpandedTranscript(2, 10, 4, 10)).toBe(true);
    expect(shouldRefreshExpandedTranscript(2, 10, 2, 20)).toBe(true);
  });

  it("returns false when the revision is unchanged", () => {
    expect(shouldRefreshExpandedTranscript(2, 10, 2, 10)).toBe(false);
  });
});
