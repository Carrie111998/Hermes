import { Plus, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";

import { usePageHeader } from "@/contexts/usePageHeader";
import { api } from "@/lib/api";
import {
  clearChatPtyToken,
  readChatPtyToken,
} from "@/lib/chat-pty-token";
import {
  MAX_CHAT_TABS,
  addChatTab,
  closeChatTab,
  restoreChatTabs,
  setChatTabResume,
  type ChatTabsState,
} from "@/lib/chat-tabs";
import { cn } from "@/lib/utils";

import ChatPage from "./ChatPage";

const CHAT_TABS_STORAGE_KEY = "hermes.chat.tabs.v1";
const TERMINATE_RETRY_DELAYS_MS = [250, 1000] as const;

function loadChatTabs(resumeSessionId: string | null): ChatTabsState {
  let restored = restoreChatTabs(null);
  if (typeof window === "undefined") return restored;
  try {
    restored = restoreChatTabs(
      window.sessionStorage.getItem(CHAT_TABS_STORAGE_KEY),
    );
  } catch {
    // Tab persistence is optional; route-driven resume still applies below.
  }
  return resumeSessionId
    ? setChatTabResume(restored, restored.activeId, resumeSessionId)
    : restored;
}

function createTabId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `tab-${crypto.randomUUID()}`;
  }
  return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export default function ChatWorkspace({ isActive = true }: { isActive?: boolean }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [state, setState] = useState<ChatTabsState>(() =>
    loadChatTabs(searchParams.get("resume")),
  );
  const [titles, setTitles] = useState<Record<string, string | null>>({});
  const { setTitle } = usePageHeader();
  const expectedResumeRef = useRef<string | null | undefined>(undefined);

  const terminateTabPty = useCallback((tabId: string, token: string) => {
    const attempt = (retryIndex: number) => {
      void api
        .terminatePty(token)
        .then(() => {
          // Only clear the credential we actually terminated. A new PTY may
          // have rotated this tab's token while the request was in flight.
          if (readChatPtyToken(tabId) === token) clearChatPtyToken(tabId);
        })
        .catch((error) => {
          const delay = TERMINATE_RETRY_DELAYS_MS[retryIndex];
          if (delay !== undefined) {
            window.setTimeout(() => attempt(retryIndex + 1), delay);
            return;
          }
          // Retain the token after the final failure so a future cleanup can
          // still identify the detached PTY; the server TTL remains a backstop.
          console.warn("[dashboard chat] PTY termination failed:", error);
        });
    };
    attempt(0);
  }, []);

  const writeActiveResumeToUrl = useCallback(
    (resumeSessionId: string | null) => {
      expectedResumeRef.current = resumeSessionId;
      const next = new URLSearchParams(searchParams);
      if (resumeSessionId) next.set("resume", resumeSessionId);
      else next.delete("resume");
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  useEffect(() => {
    const resume = searchParams.get("resume");
    if (expectedResumeRef.current !== undefined) {
      if (expectedResumeRef.current === resume) {
        expectedResumeRef.current = undefined;
        return;
      }
      expectedResumeRef.current = undefined;
    }
    setState((current) =>
      setChatTabResume(current, current.activeId, resume),
    );
  }, [searchParams]);

  useEffect(() => {
    try {
      window.sessionStorage.setItem(CHAT_TABS_STORAGE_KEY, JSON.stringify(state));
    } catch {
      // Session persistence is a convenience; tabs continue working in memory.
    }
  }, [state]);

  const activeTitle = titles[state.activeId] ?? null;

  useEffect(() => {
    if (!isActive) {
      setTitle(null);
      return;
    }
    setTitle(activeTitle);
    return () => setTitle(null);
  }, [activeTitle, isActive, setTitle]);

  const addTab = useCallback(() => {
    writeActiveResumeToUrl(null);
    setState((current) => addChatTab(current, createTabId()));
  }, [writeActiveResumeToUrl]);

  const selectTab = useCallback(
    (id: string) => {
      const target = state.tabs.find((tab) => tab.id === id);
      if (!target) return;
      writeActiveResumeToUrl(target.resumeSessionId ?? null);
      setState((current) =>
        current.activeId === id ? current : { ...current, activeId: id },
      );
    },
    [state.tabs, writeActiveResumeToUrl],
  );

  const closeTab = useCallback((id: string) => {
    const nextState = closeChatTab(state, id);
    if (nextState === state) return;
    if (state.activeId === id) {
      const nextActive = nextState.tabs.find(
        (tab) => tab.id === nextState.activeId,
      );
      writeActiveResumeToUrl(nextActive?.resumeSessionId ?? null);
    }
    setState(nextState);
    setTitles((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
    const token = readChatPtyToken(id);
    if (token) terminateTabPty(id, token);
    if (state.activeId === id) {
      document
        .querySelector<HTMLElement>(
          `[role="tab"][data-tab-id="${nextState.activeId}"]`,
        )
        ?.focus();
    }
  }, [state, terminateTabPty, writeActiveResumeToUrl]);

  const updateTabResume = useCallback(
    (id: string, resumeSessionId: string | null) => {
      setState((current) => setChatTabResume(current, id, resumeSessionId));
      if (state.activeId === id) writeActiveResumeToUrl(resumeSessionId);
    },
    [state.activeId, writeActiveResumeToUrl],
  );

  const focusTabAt = useCallback((index: number) => {
    const target = state.tabs[index];
    if (!target) return;
    selectTab(target.id);
    document
      .querySelector<HTMLElement>(`[role="tab"][data-tab-id="${target.id}"]`)
      ?.focus();
  }, [selectTab, state.tabs]);

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="Chat workspace">
      <div className="flex min-w-0 items-end border-b border-border/70 bg-muted/25 px-2 pt-1.5">
        <div
          role="tablist"
          aria-label="Chat sessions"
          className="flex min-w-0 flex-1 items-end gap-1 overflow-x-auto [scrollbar-width:thin]"
        >
          {state.tabs.map((tab, index) => {
            const selected = tab.id === state.activeId;
            const label = titles[tab.id] || `Chat ${index + 1}`;
            return (
              <div
                key={tab.id}
                className={cn(
                  "group flex h-9 min-w-28 max-w-52 shrink-0 items-center rounded-t-lg border border-b-0 px-1",
                  selected
                    ? "border-border bg-background text-foreground"
                    : "border-transparent bg-muted/50 text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                <button
                  type="button"
                  role="tab"
                  id={`chat-tab-${tab.id}`}
                  aria-controls={`chat-panel-${tab.id}`}
                  aria-selected={selected}
                  data-tab-id={tab.id}
                  tabIndex={selected ? 0 : -1}
                  className="min-w-0 flex-1 truncate px-2 text-left text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => selectTab(tab.id)}
                  onKeyDown={(event) => {
                    if (event.key === "ArrowLeft") {
                      event.preventDefault();
                      focusTabAt((index - 1 + state.tabs.length) % state.tabs.length);
                    } else if (event.key === "ArrowRight") {
                      event.preventDefault();
                      focusTabAt((index + 1) % state.tabs.length);
                    } else if (event.key === "Home") {
                      event.preventDefault();
                      focusTabAt(0);
                    } else if (event.key === "End") {
                      event.preventDefault();
                      focusTabAt(state.tabs.length - 1);
                    } else if (event.key === "Delete" && state.tabs.length > 1) {
                      event.preventDefault();
                      closeTab(tab.id);
                    }
                  }}
                >
                  {label}
                </button>
                <button
                  type="button"
                  aria-label={`Close ${label}`}
                  tabIndex={-1}
                  disabled={state.tabs.length === 1}
                  className="grid size-6 shrink-0 place-items-center rounded-md opacity-70 outline-none hover:bg-muted-foreground/15 hover:opacity-100 focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-25"
                  onClick={() => closeTab(tab.id)}
                  onMouseDown={(event) => event.preventDefault()}
                >
                  <X className="size-3.5" aria-hidden="true" />
                </button>
              </div>
            );
          })}
        </div>
        <button
          type="button"
          aria-label="New chat tab"
          title={
            state.tabs.length >= MAX_CHAT_TABS
              ? `Maximum ${MAX_CHAT_TABS} chat tabs`
              : "New chat tab"
          }
          disabled={state.tabs.length >= MAX_CHAT_TABS}
          className="mb-1 ml-1 grid size-8 shrink-0 place-items-center rounded-md text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-35"
          onClick={addTab}
        >
          <Plus className="size-4" aria-hidden="true" />
        </button>
      </div>

      <div className="relative flex min-h-0 min-w-0 flex-1">
        {state.tabs.map((tab) => {
          const selected = tab.id === state.activeId;
          const paneActive = isActive && selected;
          return (
            <div
              key={tab.id}
              id={`chat-panel-${tab.id}`}
              role="tabpanel"
              aria-labelledby={`chat-tab-${tab.id}`}
              hidden={!selected}
              className={cn("min-h-0 min-w-0 flex-1", selected ? "flex" : "hidden")}
            >
              <ChatPage
                isActive={paneActive}
                managePageHeader={false}
                ptyTabId={tab.id}
                resumeSessionId={tab.resumeSessionId ?? null}
                onResumeSessionChange={(sessionId) =>
                  updateTabResume(tab.id, sessionId)
                }
                onSessionTitleChange={(title) =>
                  setTitles((current) =>
                    current[tab.id] === title
                      ? current
                      : { ...current, [tab.id]: title },
                  )
                }
              />
            </div>
          );
        })}
      </div>
    </section>
  );
}
