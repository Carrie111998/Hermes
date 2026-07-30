import { describe, expect, it } from "vitest";

import {
  OPEN_SOCKET_READY_STATE,
  planChatLearnSeed,
} from "./chat-learn-seed";

function plan(
  search: string,
  overrides: Partial<Parameters<typeof planChatLearnSeed>[0]> = {},
) {
  return planChatLearnSeed({
    isActive: true,
    ptyState: "open",
    searchParams: new URLSearchParams(search),
    socketReadyState: OPEN_SOCKET_READY_STATE,
    ...overrides,
  });
}

describe("persistent chat learn seed delivery", () => {
  it("plans a newly navigated learn seed without requiring a socket reopen", () => {
    expect(plan("resume=session-id")).toBeNull();

    const delivery = plan("learn=source%20text&resume=session-id");

    expect(delivery?.command).toBe("/learn source text\r");
    expect(delivery?.nextSearchParams.get("learn")).toBeNull();
    expect(delivery?.nextSearchParams.get("resume")).toBe("session-id");
  });

  it("retains the seed until an active open PTY can accept it", () => {
    const search = "learn=retry-me&resume=session-id";

    expect(plan(search, { isActive: false })).toBeNull();
    expect(plan(search, { ptyState: "connecting" })).toBeNull();
    expect(plan(search, { socketReadyState: 3 })).toBeNull();
    expect(new URLSearchParams(search).get("learn")).toBe("retry-me");
  });
});
