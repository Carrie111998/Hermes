// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location-search">{location.search}</output>;
}

class FakeFitAddon {
  fit() {}
}

class FakeWebglAddon {
  onContextLoss() {
    return { dispose() {} };
  }
}

class FakeTerminal {
  options: Record<string, unknown>;
  rows = 24;
  cols = 80;
  parser = {
    registerOscHandler: vi.fn(),
  };
  unicode = { activeVersion: "" };

  constructor(options: Record<string, unknown>) {
    this.options = options;
  }

  attachCustomKeyEventHandler() {
    return true;
  }

  attachCustomWheelEventHandler() {
    return true;
  }

  clearSelection() {}

  dispose() {}

  focus() {}

  getSelection() {
    return "";
  }

  loadAddon() {}

  onData() {
    return { dispose() {} };
  }

  onResize() {
    return { dispose() {} };
  }

  open() {}

  paste() {}

  refresh() {}

  write() {}
}

const maybeReloadForLoopbackWsAuthFailure = vi.fn(() => false);

vi.mock("@xterm/addon-fit", () => ({ FitAddon: FakeFitAddon }));
vi.mock("@xterm/addon-unicode11", () => ({ Unicode11Addon: class {} }));
vi.mock("@xterm/addon-web-links", () => ({ WebLinksAddon: class {} }));
vi.mock("@xterm/addon-webgl", () => ({ WebglAddon: FakeWebglAddon }));
vi.mock("@xterm/xterm", () => ({ Terminal: FakeTerminal }));
vi.mock("@/components/ChatSidebar", () => ({
  ChatSidebar: () => null,
}));
vi.mock("@/components/ChatSessionList", () => ({
  ChatSessionList: () => null,
}));
vi.mock("@/components/Backdrop", () => ({ Backdrop: () => null }));
vi.mock("@/plugins", () => ({
  PluginSlot: () => null,
}));
vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setEnd: vi.fn(), setTitle: vi.fn() }),
}));
vi.mock("@/contexts/useProfileScope", () => ({
  useProfileScope: () => ({ profile: "" }),
}));
vi.mock("@/themes", () => ({
  useTheme: () => ({ theme: { terminalBackground: "#000000" } }),
}));
vi.mock("@/i18n", () => ({
  useI18n: () => ({
    t: {
      app: {
        closeModelTools: "Close model tools",
        modelToolsSheetSubtitle: "Tools",
        modelToolsSheetTitle: "Model",
        workspaceStart: {
          preparing: "Hermes bereitet Worktree, Preflight und Session vor …",
          failed:
            "Der Task-Workspace konnte nicht vorbereitet werden. Es wurde kein Run gestartet.",
          retry: "Workspace-Start erneut versuchen",
          failurePrefix: "Workspace-Start fehlgeschlagen",
          evidenceFailurePrefix:
            "Chat verbunden, aber der Verbindungsnachweis konnte nicht gespeichert werden",
        },
      },
    },
  }),
}));
vi.mock("@/lib/dashboard-auth-reload", () => ({
  attemptDashboardTokenReloadOnce: vi.fn(() => false),
  clearDashboardTokenReloadAttempt: vi.fn(),
  maybeReloadForLoopbackWsAuthFailure,
}));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;

  binaryType = "blob";
  onclose: ((event: CloseEventLike) => void) | null = null;
  onmessage: ((event: { data: ArrayBuffer | string }) => void) | null = null;
  onopen: (() => void) | null = null;
  readyState = FakeWebSocket.OPEN;
  url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  close() {
    this.readyState = 3;
  }

  send() {}
}

type CloseEventLike = {
  code: number;
  reason: string;
  wasClean: boolean;
};

let container: HTMLDivElement;
let root: Root;

async function render(ui: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(ui));
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  maybeReloadForLoopbackWsAuthFailure.mockClear();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      disconnect() {}
      observe() {}
      unobserve() {}
    },
  );
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    cb(0);
    return 1;
  });
  vi.stubGlobal("cancelAnimationFrame", () => {});
  vi.stubGlobal("matchMedia", () => ({
    addEventListener() {},
    matches: false,
    media: "",
    removeEventListener() {},
  }));
  vi.stubGlobal("crypto", {
    getRandomValues: (values: Uint8Array) => {
      values.fill(7);
      return values;
    },
    randomUUID: () => "chat-test-id",
  });

  Object.defineProperty(window, "visualViewport", {
    configurable: true,
    value: { addEventListener() {}, removeEventListener() {}, width: 1280 },
  });
  Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
    configurable: true,
    value: "stale-token",
    writable: true,
  });
  Object.defineProperty(window, "__HERMES_AUTH_REQUIRED__", {
    configurable: true,
    value: false,
    writable: true,
  });
  Object.defineProperty(window.navigator, "clipboard", {
    configurable: true,
    value: {
      readText: vi.fn(async () => ""),
      writeText: vi.fn(async () => {}),
    },
  });
  sessionStorage.clear();
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  vi.unstubAllGlobals();
});

describe("ChatPage", () => {
  it("treats loopback 4401 closes as stale-token reload candidates", async () => {
    const { default: ChatPage } = await import("./ChatPage");

    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
      </MemoryRouter>,
    );

    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    FakeWebSocket.instances[0].onclose?.({
      code: 4401,
      reason: "auth: token_mismatch",
      wasClean: true,
    });

    expect(maybeReloadForLoopbackWsAuthFailure).toHaveBeenCalledWith(4401);
  });

  it("prepares a task workspace before opening the exact persisted session", async () => {
    let resolveFetch!: (response: Response) => void;
    const responsePromise = new Promise<Response>((resolve) => {
      resolveFetch = resolve;
    });
    const fetchMock = vi.fn<
      (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
    >(() => responsePromise);
    vi.stubGlobal("fetch", fetchMock);
    const { default: ChatPage } = await import("./ChatPage");

    await render(
      <MemoryRouter
        initialEntries={[
          "/chat?profile=goliath-main&start_project=p_fixture&start_task=t_fixture&start_workstream=W1.1&start_key=p_fixture%3At_fixture%3AW1.1%3Av1&write_scope=dashboard",
        ]}
      >
        <ChatPage isActive />
        <LocationProbe />
      </MemoryRouter>,
    );

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(container.textContent).toContain(
      "Hermes bereitet Worktree, Preflight und Session vor …",
    );
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "/api/workspaces/interactive/start?profile=goliath-main",
    );
    expect(JSON.parse(String(init?.body))).toEqual({
      project_id: "p_fixture",
      task_id: "t_fixture",
      workstream_id: "W1.1",
      idempotency_key: "p_fixture:t_fixture:W1.1:v1",
      write_scope: "dashboard",
    });

    await act(async () => {
      resolveFetch({
        ok: true,
        status: 200,
        json: async () => ({
          ok: true,
          project_id: "p_fixture",
          task_id: "t_fixture",
          workstream_id: "W1.1",
          session_id: "20260805_120000_ab12cd",
          repo_root: "/repo",
          workspace_path: "/repo/.worktrees/t_fixture",
          branch: "feat/browser-session",
          base_ref: "origin/main",
          preflight_status: "passed",
          preflight_summary: "PREFLIGHT_OK",
          reused: false,
        }),
        text: async () => "",
      } as Response);
      await responsePromise;
    });

    await vi.waitFor(() =>
      expect(container?.textContent).not.toContain(
        "Hermes bereitet Worktree, Preflight und Session vor …",
      ),
    );
    expect(container?.textContent).not.toContain("Workspace-Start fehlgeschlagen:");
    await vi.waitFor(() =>
      expect(
        container?.querySelector('[data-testid="location-search"]')?.textContent,
      ).toContain("resume=20260805_120000_ab12cd"),
    );
    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(FakeWebSocket.instances[0].url).toContain(
      "resume=20260805_120000_ab12cd",
    );
    expect(FakeWebSocket.instances[0].url).toContain("profile=goliath-main");
    await act(async () => {
      FakeWebSocket.instances[0].onmessage?.({
        data: "\r\n\u001b[31mChat unavailable: pty failed\u001b[0m\r\n",
      });
      await Promise.resolve();
    });
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).includes("/api/workspaces/interactive/connected"),
      ),
    ).toHaveLength(0);
    await act(async () => {
      FakeWebSocket.instances[0].onmessage?.({ data: "PTY ready" });
      FakeWebSocket.instances[0].onmessage?.({ data: "second frame" });
      await Promise.resolve();
    });
    await vi.waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([url]) =>
          String(url).includes("/api/workspaces/interactive/connected"),
        ),
      ).toHaveLength(1),
    );
    const [connectedUrl, connectedInit] = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/workspaces/interactive/connected"),
    )!;
    expect(connectedUrl).toBe(
      "/api/workspaces/interactive/connected?profile=goliath-main",
    );
    expect(JSON.parse(String(connectedInit?.body))).toMatchObject({
      project_id: "p_fixture",
      task_id: "t_fixture",
      workstream_id: "W1.1",
      idempotency_key: "p_fixture:t_fixture:W1.1:v1",
      session_id: "20260805_120000_ab12cd",
    });
  });

  it("shows localized, keyboard-reachable workspace failure recovery", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            detail: {
              code: "preflight_failed",
              message: "Preflight fehlgeschlagen",
            },
          }),
          {
            status: 422,
            headers: { "content-type": "application/json" },
          },
        ),
      ),
    );
    const { default: ChatPage } = await import("./ChatPage");

    await render(
      <MemoryRouter
        initialEntries={[
          "/chat?profile=goliath-main&start_project=p_fixture&start_task=t_fixture&start_workstream=W1.1&start_key=p_fixture%3At_fixture%3AW1.1%3Av1&write_scope=dashboard",
        ]}
      >
        <ChatPage isActive />
      </MemoryRouter>,
    );

    await vi.waitFor(() =>
      expect(container.textContent).toContain("Workspace-Start fehlgeschlagen:"),
    );
    expect(container.textContent).toContain(
      "Der Task-Workspace konnte nicht vorbereitet werden. Es wurde kein Run gestartet.",
    );
    const retry = [...container.querySelectorAll("button")].find(
      (button) => button.textContent === "Workspace-Start erneut versuchen",
    );
    expect(retry).toBeDefined();
    expect(retry?.disabled).toBe(false);
    retry?.focus();
    expect(document.activeElement).toBe(retry);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });
});
