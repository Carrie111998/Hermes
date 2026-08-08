import { describe, expect, it } from "vitest";

import {
  MAX_CHAT_TABS,
  addChatTab,
  closeChatTab,
  createInitialChatTabs,
  restoreChatTabs,
  setChatTabResume,
  type ChatTabsState,
} from "./chat-tabs";

describe("chat tabs", () => {
  it("starts with one stable primary tab", () => {
    expect(createInitialChatTabs()).toEqual({
      activeId: "primary",
      tabs: [{ id: "primary" }],
    });
  });

  it("adds and activates independent tabs up to the cap", () => {
    let state = createInitialChatTabs();
    for (let index = 2; index <= MAX_CHAT_TABS + 1; index += 1) {
      state = addChatTab(state, `tab-${index}`);
    }

    expect(state.tabs).toHaveLength(MAX_CHAT_TABS);
    expect(state.activeId).toBe(`tab-${MAX_CHAT_TABS}`);
    expect(new Set(state.tabs.map((tab) => tab.id)).size).toBe(MAX_CHAT_TABS);
  });

  it("selects the neighboring tab when the active tab closes", () => {
    const state: ChatTabsState = {
      activeId: "tab-2",
      tabs: [{ id: "primary" }, { id: "tab-2" }, { id: "tab-3" }],
    };

    expect(closeChatTab(state, "tab-2")).toEqual({
      activeId: "tab-3",
      tabs: [{ id: "primary" }, { id: "tab-3" }],
    });
  });

  it("keeps resume identity local to each tab", () => {
    const primary = setChatTabResume(
      createInitialChatTabs(),
      "primary",
      "session-a",
    );
    const second = addChatTab(primary, "tab-2");
    const resumed = setChatTabResume(second, "tab-2", "session-b");

    expect(resumed.tabs).toEqual([
      { id: "primary", resumeSessionId: "session-a" },
      { id: "tab-2", resumeSessionId: "session-b" },
    ]);
  });

  it("never closes the final tab", () => {
    const state = createInitialChatTabs();
    expect(closeChatTab(state, "primary")).toEqual(state);
  });

  it("restores only bounded, valid tab identifiers", () => {
    expect(
      restoreChatTabs(
        JSON.stringify({
          activeId: "tab-2",
          tabs: [
            { id: "primary" },
            { id: "tab-2" },
            { id: "bad id" },
            { id: "tab-2" },
          ],
        }),
      ),
    ).toEqual({
      activeId: "tab-2",
      tabs: [{ id: "primary" }, { id: "tab-2" }],
    });
    expect(restoreChatTabs("not-json")).toEqual(createInitialChatTabs());
  });
});
