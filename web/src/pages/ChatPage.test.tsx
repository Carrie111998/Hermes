// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

class FakeFitAddon {
  fit() {}
}

class FakeWebglAddon {
  onContextLoss() {
    return { dispose() {} };
  }
}

type WebLinkHandler = (event: MouseEvent, uri: string) => void;
let webLinkHandler: WebLinkHandler | null = null;

class FakeWebLinksAddon {
  constructor(handler?: WebLinkHandler) {
    webLinkHandler = handler ?? null;
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
const setPageTitle = vi.fn();

vi.mock("@xterm/addon-fit", () => ({ FitAddon: FakeFitAddon }));
vi.mock("@xterm/addon-unicode11", () => ({ Unicode11Addon: class {} }));
vi.mock("@xterm/addon-web-links", () => ({ WebLinksAddon: FakeWebLinksAddon }));
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
  usePageHeader: () => ({ setEnd: vi.fn(), setTitle: setPageTitle }),
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
      },
    },
  }),
}));
vi.mock("@/lib/dashboard-auth-reload", () => ({
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
let root: Root | undefined;

async function render(ui: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  const nextRoot = createRoot(container);
  root = nextRoot;
  await act(async () => nextRoot.render(ui));
}

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {`${location.pathname}${location.search}`}
    </output>
  );
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  FakeWebSocket.instances = [];
  webLinkHandler = null;
  maybeReloadForLoopbackWsAuthFailure.mockClear();
  setPageTitle.mockClear();
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
  root = undefined;
  container?.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ChatPage", () => {
  it("does not open a websocket after unmount while its URL is pending", async () => {
    let resolveUrl: ((url: string) => void) | undefined;
    const { api } = await import("@/lib/api");
    vi.spyOn(api, "buildWsUrl").mockImplementation(
      () => new Promise<string>((resolve) => { resolveUrl = resolve; }),
    );
    const { default: ChatPage } = await import("./ChatPage");

    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
      </MemoryRouter>,
    );
    await vi.waitFor(() => expect(resolveUrl).toBeTypeOf("function"));
    await act(async () => root?.unmount());
    root = undefined;
    resolveUrl?.("ws://localhost/api/pty");
    await act(async () => { await Promise.resolve(); });

    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("does not mutate the page title when the workspace owns the header", async () => {
    const { default: ChatPage } = await import("./ChatPage");

    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive managePageHeader={false} />
      </MemoryRouter>,
    );

    expect(setPageTitle).not.toHaveBeenCalled();
  });

  it("routes same-origin dashboard links through React Router", async () => {
    const { default: ChatPage } = await import("./ChatPage");

    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
        <LocationProbe />
      </MemoryRouter>,
    );

    await vi.waitFor(() => expect(webLinkHandler).not.toBeNull());
    const preventDefault = vi.fn();
    const dashboardUrl = new URL("/models", window.location.href).href;

    await act(async () => {
      webLinkHandler?.({ preventDefault } as unknown as MouseEvent, dashboardUrl);
    });

    expect(preventDefault).toHaveBeenCalledOnce();
    expect(container.querySelector('[data-testid="location"]')?.textContent).toBe(
      "/models",
    );
  });

  it("suppresses rejected same-origin links instead of opening them", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const { default: ChatPage } = await import("./ChatPage");
    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
      </MemoryRouter>,
    );
    await vi.waitFor(() => expect(webLinkHandler).not.toBeNull());
    const preventDefault = vi.fn();

    await act(async () => {
      webLinkHandler?.(
        { preventDefault } as unknown as MouseEvent,
        new URL("/models?token=secret", window.location.href).href,
      );
    });

    expect(preventDefault).toHaveBeenCalledOnce();
    expect(open).not.toHaveBeenCalled();
    open.mockRestore();
  });

  it("opens explicitly safe external HTTP links", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const { default: ChatPage } = await import("./ChatPage");
    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
      </MemoryRouter>,
    );
    await vi.waitFor(() => expect(webLinkHandler).not.toBeNull());
    const preventDefault = vi.fn();

    await act(async () => {
      webLinkHandler?.(
        { preventDefault } as unknown as MouseEvent,
        "https://example.com/docs",
      );
    });

    expect(preventDefault).toHaveBeenCalledOnce();
    expect(open).toHaveBeenCalledWith(
      "https://example.com/docs",
      "_blank",
      "noopener,noreferrer",
    );
    open.mockRestore();
  });

  it("treats loopback 4401 closes as stale-token reload candidates", async () => {
    const { default: ChatPage } = await import("./ChatPage");

    await render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatPage isActive />
      </MemoryRouter>,
    );

    await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));

    await act(async () => {
      FakeWebSocket.instances[0].onclose?.({
        code: 4401,
        reason: "auth: token_mismatch",
        wasClean: true,
      });
    });

    expect(maybeReloadForLoopbackWsAuthFailure).toHaveBeenCalledWith(4401);
  });
});
