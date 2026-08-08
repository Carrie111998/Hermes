// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const terminatePty = vi.fn(async () => undefined);

vi.mock("@/lib/api", () => ({
  api: { terminatePty },
}));

vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setEnd: vi.fn(), setTitle: vi.fn() }),
}));

vi.mock("./ChatPage", () => ({
  default: ({
    isActive,
    ptyTabId,
  }: {
    isActive?: boolean;
    ptyTabId?: string;
  }) => (
    <div data-chat-pane={ptyTabId} data-active={isActive ? "true" : "false"} />
  ),
}));

let container: HTMLDivElement;
let root: Root;

function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (key) => data.get(key) ?? null,
    key: (index) => [...data.keys()][index] ?? null,
    removeItem: (key) => void data.delete(key),
    setItem: (key, value) => void data.set(key, String(value)),
  };
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  terminatePty.mockClear();
  terminatePty.mockResolvedValue(undefined);
  vi.stubGlobal("localStorage", memoryStorage());
  sessionStorage.clear();
  localStorage.clear();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function renderWorkspace() {
  const { default: ChatWorkspace } = await import("./ChatWorkspace");
  await act(async () =>
    root.render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatWorkspace isActive />
      </MemoryRouter>,
    ),
  );
}

function click(element: Element | null) {
  if (!element) throw new Error("Expected clickable element");
  element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

function keyDown(element: Element | null, key: string) {
  if (!element) throw new Error("Expected keyboard target");
  element.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key }));
}

describe("ChatWorkspace", () => {
  it("adds a browser-style tab while keeping both chat panes mounted", async () => {
    await renderWorkspace();

    expect(container.querySelector('[role="tablist"]')).not.toBeNull();
    expect(container.querySelectorAll('[role="tab"]')).toHaveLength(1);

    await act(async () => click(container.querySelector('[aria-label="New chat tab"]')));

    const tabs = container.querySelectorAll('[role="tab"]');
    expect(tabs).toHaveLength(2);
    expect(tabs[1].getAttribute("aria-selected")).toBe("true");
    expect(container.querySelectorAll("[data-chat-pane]")).toHaveLength(2);
    expect(
      container.querySelector('[data-chat-pane="primary"]')?.getAttribute("data-active"),
    ).toBe("false");
  });

  it("switches sessions without unmounting either pane", async () => {
    await renderWorkspace();
    await act(async () => click(container.querySelector('[aria-label="New chat tab"]')));
    await act(async () => click(container.querySelector('[role="tab"]')));

    expect(container.querySelectorAll("[data-chat-pane]")).toHaveLength(2);
    expect(
      container.querySelector('[data-chat-pane="primary"]')?.getAttribute("data-active"),
    ).toBe("true");
  });

  it("closes a tab and requests termination of its PTY", async () => {
    await renderWorkspace();
    await act(async () => click(container.querySelector('[aria-label="New chat tab"]')));
    const active = container.querySelector('[role="tab"][aria-selected="true"]');
    const activeId = active?.getAttribute("data-tab-id");
    if (!activeId) throw new Error("Expected active tab id");
    localStorage.setItem(`hermes.pty.token.chat.${activeId}`, "attach-token");

    await act(async () =>
      click(container.querySelector(`[aria-label="Close Chat 2"]`)),
    );

    expect(container.querySelectorAll('[role="tab"]')).toHaveLength(1);
    expect(container.querySelectorAll("[data-chat-pane]")).toHaveLength(1);
    expect(terminatePty).toHaveBeenCalledWith("attach-token");
    expect(localStorage.getItem(`hermes.pty.token.chat.${activeId}`)).toBeNull();
    expect(document.activeElement?.getAttribute("role")).toBe("tab");
    expect(document.activeElement?.getAttribute("aria-selected")).toBe("true");
  });

  it("supports APG arrow, Home, End, and Delete keyboard behavior", async () => {
    await renderWorkspace();
    await act(async () => click(container.querySelector('[aria-label="New chat tab"]')));
    await act(async () => click(container.querySelector('[aria-label="New chat tab"]')));
    const tabs = () => container.querySelectorAll('[role="tab"]');

    await act(async () => keyDown(tabs()[2], "Home"));
    expect(document.activeElement).toBe(tabs()[0]);
    expect(tabs()[0].getAttribute("aria-selected")).toBe("true");

    await act(async () => keyDown(tabs()[0], "ArrowLeft"));
    expect(document.activeElement).toBe(tabs()[2]);

    await act(async () => keyDown(tabs()[2], "Delete"));
    expect(tabs()).toHaveLength(2);
    expect(document.activeElement?.getAttribute("role")).toBe("tab");
    expect(document.activeElement?.getAttribute("aria-selected")).toBe("true");

    await act(async () => keyDown(tabs()[1], "End"));
    expect(document.activeElement).toBe(tabs()[1]);
  });

  it("keeps inactive close buttons out of the tab order without stealing focus", async () => {
    await renderWorkspace();
    await act(async () => click(container.querySelector('[aria-label="New chat tab"]')));
    await act(async () => click(container.querySelector('[aria-label="New chat tab"]')));
    const firstTab = container.querySelector<HTMLElement>('[role="tab"]');
    await act(async () => click(firstTab));
    firstTab?.focus();
    const inactiveClose = container.querySelector<HTMLElement>(
      '[aria-label="Close Chat 2"]',
    );

    expect(inactiveClose?.tabIndex).toBe(-1);
    await act(async () => click(inactiveClose));

    expect(container.querySelectorAll('[role="tab"]')).toHaveLength(2);
    expect(firstTab?.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(firstTab);
  });

  it("retains the attach token when termination retries fail", async () => {
    vi.useFakeTimers();
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    terminatePty.mockRejectedValue(new Error("offline"));
    await renderWorkspace();
    await act(async () => click(container.querySelector('[aria-label="New chat tab"]')));
    const active = container.querySelector('[role="tab"][aria-selected="true"]');
    const activeId = active?.getAttribute("data-tab-id");
    if (!activeId) throw new Error("Expected active tab id");
    const tokenKey = `hermes.pty.token.chat.${activeId}`;
    localStorage.setItem(tokenKey, "attach-token");

    await act(async () => click(container.querySelector('[aria-label="Close Chat 2"]')));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1250);
    });

    expect(terminatePty).toHaveBeenCalledTimes(3);
    expect(localStorage.getItem(tokenKey)).toBe("attach-token");
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});
