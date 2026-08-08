import { describe, expect, it } from "vitest";
import {
  MANAGEMENT_PATH,
  managementTabFromSearch,
  shouldScrollToBottomOnChatActivation,
} from "./chat-navigation";

describe("chat navigation", () => {
  it("recognizes only an inactive-to-active chat transition as a bottom-scroll", () => {
    expect(shouldScrollToBottomOnChatActivation(false, true)).toBe(true);
    expect(shouldScrollToBottomOnChatActivation(true, true)).toBe(false);
    expect(shouldScrollToBottomOnChatActivation(false, false)).toBe(false);
  });

  it("provides one management entry point with stable profile/settings tabs", () => {
    expect(MANAGEMENT_PATH).toBe("/manage");
    expect(managementTabFromSearch("")).toBe("profiles");
    expect(managementTabFromSearch("?tab=settings")).toBe("settings");
    expect(managementTabFromSearch("?tab=unknown")).toBe("profiles");
  });
});
