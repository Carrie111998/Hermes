export const MAX_CHAT_TABS = 8;

const CHAT_TAB_ID = /^[A-Za-z0-9._-]{1,128}$/;

export type ChatTab = {
  id: string;
  resumeSessionId?: string;
};

export type ChatTabsState = {
  activeId: string;
  tabs: ChatTab[];
};

export function createInitialChatTabs(): ChatTabsState {
  return { activeId: "primary", tabs: [{ id: "primary" }] };
}

export function addChatTab(
  state: ChatTabsState,
  id: string,
  resumeSessionId?: string,
): ChatTabsState {
  if (
    state.tabs.length >= MAX_CHAT_TABS ||
    !CHAT_TAB_ID.test(id) ||
    state.tabs.some((tab) => tab.id === id)
  ) {
    return state;
  }
  return {
    activeId: id,
    tabs: [...state.tabs, { id, ...(resumeSessionId ? { resumeSessionId } : {}) }],
  };
}

export function setChatTabResume(
  state: ChatTabsState,
  id: string,
  resumeSessionId: string | null,
): ChatTabsState {
  let changed = false;
  const tabs = state.tabs.map((tab) => {
    if (tab.id !== id || (tab.resumeSessionId ?? null) === resumeSessionId) {
      return tab;
    }
    changed = true;
    const { resumeSessionId: _old, ...rest } = tab;
    return resumeSessionId ? { ...rest, resumeSessionId } : rest;
  });
  return changed ? { ...state, tabs } : state;
}

export function closeChatTab(
  state: ChatTabsState,
  id: string,
): ChatTabsState {
  if (state.tabs.length <= 1) return state;
  const index = state.tabs.findIndex((tab) => tab.id === id);
  if (index < 0) return state;
  const tabs = state.tabs.filter((tab) => tab.id !== id);
  const activeId =
    state.activeId === id
      ? tabs[Math.min(index, tabs.length - 1)].id
      : state.activeId;
  return { activeId, tabs };
}

export function restoreChatTabs(raw: string | null): ChatTabsState {
  if (!raw) return createInitialChatTabs();
  try {
    const parsed = JSON.parse(raw) as Partial<ChatTabsState>;
    if (!Array.isArray(parsed.tabs)) return createInitialChatTabs();
    const seen = new Set<string>();
    const tabs: ChatTab[] = [];
    for (const candidate of parsed.tabs) {
      const id = candidate?.id;
      if (
        typeof id !== "string" ||
        !CHAT_TAB_ID.test(id) ||
        seen.has(id)
      ) {
        continue;
      }
      seen.add(id);
      const resumeSessionId =
        typeof candidate.resumeSessionId === "string" &&
        candidate.resumeSessionId.trim()
          ? candidate.resumeSessionId
          : undefined;
      tabs.push({ id, ...(resumeSessionId ? { resumeSessionId } : {}) });
      if (tabs.length === MAX_CHAT_TABS) break;
    }
    if (tabs.length === 0) return createInitialChatTabs();
    const activeId =
      typeof parsed.activeId === "string" &&
      tabs.some((tab) => tab.id === parsed.activeId)
        ? parsed.activeId
        : tabs[0].id;
    return { activeId, tabs };
  } catch {
    return createInitialChatTabs();
  }
}
