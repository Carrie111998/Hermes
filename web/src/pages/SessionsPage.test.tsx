// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getSessions: vi.fn(),
  getSessionMessages: vi.fn(),
  getEmptySessionsCount: vi.fn(),
  getStatus: vi.fn(),
  searchSessions: vi.fn(),
  importSessions: vi.fn(),
  exportSessionUrl: vi.fn(),
  renameSession: vi.fn(),
  pruneSessions: vi.fn(),
  deleteSession: vi.fn(),
  deleteEmptySessions: vi.fn(),
  bulkDeleteSessions: vi.fn(),
  getProfiles: vi.fn(),
  getActiveProfile: vi.fn(),
  getSessionStats: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
  // ProfileProvider mirrors its selection into the api module; the mock
  // keeps that import resolvable.
  setManagementProfile: vi.fn(),
  getManagementProfile: vi.fn(() => ""),
}));
vi.mock("@/components/PlatformsCard", () => ({
  PlatformsCard: () => null,
}));
vi.mock("@/components/Markdown", () => ({
  Markdown: () => null,
}));

let container: HTMLDivElement;
let root: Root;

// Route React updates through act() without warnings (same flag ChatPage's
// test sets).
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

async function render(ui: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(ui));
}

async function waitFor(cond: () => boolean, timeoutMs = 5000) {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error("waitFor: condition never became true");
    }
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  }
}

async function loadPageProviders() {
  const { I18nProvider } = await import("@/i18n");
  const { SystemActionsProvider } = await import("@/contexts/SystemActions");
  const { ProfileProvider } = await import("@/contexts/ProfileProvider");
  const { PageHeaderProvider } = await import("@/contexts/PageHeaderProvider");
  // Mirrors the provider nesting App uses around the routed pages.
  return function PageProviders({ children }: { children: ReactNode }) {
    return (
      <I18nProvider>
        <MemoryRouter>
          <SystemActionsProvider>
            <ProfileProvider>
              <PageHeaderProvider>{children}</PageHeaderProvider>
            </ProfileProvider>
          </SystemActionsProvider>
        </MemoryRouter>
      </I18nProvider>
    );
  };
}

function clickButton(el: Element) {
  el.dispatchEvent(
    new MouseEvent("click", { bubbles: true, cancelable: true }),
  );
}

function findRowDeleteButton(): HTMLButtonElement {
  const button = document.querySelector<HTMLButtonElement>(
    'button[aria-label="Delete session"]',
  );
  if (!button) throw new Error("row delete button not rendered");
  return button;
}

function findConfirmButton(): HTMLButtonElement {
  const dialog = document.querySelector('[role="alertdialog"]');
  if (!dialog) throw new Error("confirm dialog not open");
  const buttons = Array.from(
    dialog.querySelectorAll<HTMLButtonElement>("button"),
  );
  const confirm = buttons.find(
    (b) => b.textContent?.trim() === "Delete",
  );
  if (!confirm) throw new Error("confirm button not found");
  return confirm;
}

function mockSessionsPage(rows: Record<string, unknown>[]) {
  // Page list uses limit 20; the overview tab's recent-cards fetch uses 50.
  // Keep the overview empty so the list view (with row delete buttons)
  // renders by default.
  apiMocks.getSessions.mockImplementation(async (limit: number) => ({
    sessions: limit >= 50 ? [] : rows,
    total: limit >= 50 ? 0 : rows.length,
    limit,
    offset: 0,
  }));
}

function makeSession(overrides: Record<string, unknown> = {}) {
  return {
    id: "sid-1",
    source: "cli",
    model: null,
    title: "Managed session",
    started_at: Math.floor(Date.now() / 1000) - 60,
    ended_at: null,
    last_active: Math.floor(Date.now() / 1000),
    is_active: false,
    message_count: 2,
    tool_call_count: 0,
    input_tokens: 10,
    output_tokens: 10,
    preview: "hello",
    parent_session_id: null,
    ...overrides,
  };
}

beforeEach(() => {
  apiMocks.getSessions.mockReset();
  apiMocks.getSessions.mockResolvedValue({
    sessions: [],
    total: 0,
    limit: 20,
    offset: 0,
  });
  apiMocks.getStatus.mockReset();
  apiMocks.getStatus.mockResolvedValue({});
  apiMocks.getEmptySessionsCount.mockReset();
  apiMocks.getEmptySessionsCount.mockResolvedValue({ count: 0 });
  apiMocks.deleteSession.mockReset();
  apiMocks.deleteSession.mockResolvedValue({ ok: true });
  apiMocks.bulkDeleteSessions.mockReset();
  apiMocks.bulkDeleteSessions.mockResolvedValue({ ok: true, deleted: 0 });
  apiMocks.getProfiles.mockReset();
  apiMocks.getProfiles.mockResolvedValue({ profiles: [] });
  apiMocks.getActiveProfile.mockReset();
  // active === current keeps the management profile empty (""), which is
  // exactly the issue's precondition for the misdirected delete.
  apiMocks.getActiveProfile.mockResolvedValue({
    current: "default",
    active: "default",
  });
  apiMocks.getSessionStats.mockReset();
  apiMocks.getSessionStats.mockResolvedValue({ by_source: {} });

  vi.stubGlobal(
    "ResizeObserver",
    class {
      disconnect() {}
      observe() {}
      unobserve() {}
    },
  );
  // gsap drives its ticker through rAF; invoking the callback synchronously
  // makes its tick loop recurse to death, so schedule it as a macrotask.
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    return setTimeout(() => cb(0), 0) as unknown as number;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => {
    clearTimeout(id);
  });
  vi.stubGlobal("matchMedia", () => ({
    addEventListener() {},
    matches: false,
    media: "",
    removeEventListener() {},
  }));
  sessionStorage.clear();
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  vi.unstubAllGlobals();
});

describe("SessionsPage session deletion routing", () => {
  it("deletes a cross-profile row against its owning profile, not the management default", async () => {
    mockSessionsPage([makeSession({ id: "sid-guanli", profile: "guanli" })]);

    const [{ default: SessionsPage }, Providers] = await Promise.all([
      import("./SessionsPage"),
      loadPageProviders(),
    ]);
    await render(
      <Providers>
        <SessionsPage />
      </Providers>,
    );

    await waitFor(() => Boolean(findRowDeleteButton()));
    await act(async () => {
      clickButton(findRowDeleteButton());
    });
    await waitFor(() => Boolean(document.querySelector('[role="alertdialog"]')));
    await act(async () => {
      clickButton(findConfirmButton());
    });

    expect(apiMocks.deleteSession).toHaveBeenCalledWith(
      "sid-guanli",
      "guanli",
    );
  });

  it("falls back to the management profile when the row carries no profile stamp", async () => {
    mockSessionsPage([makeSession({ id: "sid-unstamped" })]);

    const [{ default: SessionsPage }, Providers] = await Promise.all([
      import("./SessionsPage"),
      loadPageProviders(),
    ]);
    await render(
      <Providers>
        <SessionsPage />
      </Providers>,
    );

    await waitFor(() => Boolean(findRowDeleteButton()));
    await act(async () => {
      clickButton(findRowDeleteButton());
    });
    await waitFor(() => Boolean(document.querySelector('[role="alertdialog"]')));
    await act(async () => {
      clickButton(findConfirmButton());
    });

    expect(apiMocks.deleteSession).toHaveBeenCalledWith(
      "sid-unstamped",
      undefined,
    );
  });
});
