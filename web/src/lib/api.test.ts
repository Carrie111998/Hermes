// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, fetchJSON } from "./api";

const reloadMocks = vi.hoisted(() => ({
  attemptDashboardTokenReloadOnce: vi.fn(() => false),
  clearDashboardTokenReloadAttempt: vi.fn(),
}));

vi.mock("./dashboard-auth-reload", () => ({
  attemptDashboardTokenReloadOnce: reloadMocks.attemptDashboardTokenReloadOnce,
  clearDashboardTokenReloadAttempt: reloadMocks.clearDashboardTokenReloadAttempt,
}));

const SESSION_HEADER = "X-Hermes-Session-Token";

beforeEach(() => {
  reloadMocks.attemptDashboardTokenReloadOnce.mockReset();
  reloadMocks.attemptDashboardTokenReloadOnce.mockReturnValue(false);
  reloadMocks.clearDashboardTokenReloadAttempt.mockReset();

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
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function jsonFetchMock(body: unknown = { ok: true }) {
  return vi.fn<typeof fetch>(
    async () =>
      new Response(JSON.stringify(body), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
  );
}

describe("fetchJSON", () => {
  it("tries the one-shot reload path for loopback 401s", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        clone: () => ({
          json: async () => ({}),
        }),
        ok: false,
        status: 401,
        statusText: "Unauthorized",
        text: async () => "Unauthorized",
      })),
    );
    reloadMocks.attemptDashboardTokenReloadOnce.mockReturnValue(true);

    const pending = fetchJSON("/api/status");
    await expect(Promise.race([pending, Promise.resolve("pending")])).resolves.toBe(
      "pending",
    );

    expect(reloadMocks.attemptDashboardTokenReloadOnce).toHaveBeenCalledTimes(1);
    expect(reloadMocks.clearDashboardTokenReloadAttempt).not.toHaveBeenCalled();
  });

  it("clears the reload latch after a successful response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        json: async () => ({ ok: true }),
        ok: true,
        status: 200,
      })),
    );

    await expect(fetchJSON("/api/status")).resolves.toEqual({ ok: true });

    expect(reloadMocks.clearDashboardTokenReloadAttempt).toHaveBeenCalledTimes(1);
  });
});

describe("api.getModelOptions", () => {
  it("requests a live model refresh when asked", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("keeps explicit profile scoping when refreshing", async () => {
    vi.stubGlobal("window", {});

    const fetchMock = jsonFetchMock({ providers: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getModelOptions({ profile: "default", refresh: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/options?profile=default&refresh=1&include_unconfigured=1",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});

describe("api.getSessionMessages", () => {
  it("preserves the legacy explicit-profile call signature", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ session_id: "one", messages: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getSessionMessages("one", "worker");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions/one/messages?limit=500&order=latest&profile=worker",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("requests the newest full-history page including compacted messages", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ session_id: "session/one", messages: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getSessionMessages(
      "session/one",
      { includeCompacted: true, order: "latest", limit: 500 },
      "worker",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions/session%2Fone/messages?limit=500&order=latest&include_compacted=true&profile=worker",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("supports explicit backward pagination offsets", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ session_id: "one", messages: [] });
    vi.stubGlobal("fetch", fetchMock);

    await api.getSessionMessages(
      "one",
      { includeCompacted: true, order: "latest", limit: 200, offset: 300 },
      "worker",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/sessions/one/messages?limit=200&order=latest&offset=300&include_compacted=true&profile=worker",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("uses the prior oldest-row cursor when the tail grows between pages", async () => {
    vi.stubGlobal("window", {});
    const rows = Array.from({ length: 10 }, (_, index) => ({
      id: index + 1,
      role: "user",
      content: `msg-${index + 1}`,
    }));
    const fetchMock = vi.fn(async (rawUrl: string) => {
      const url = new URL(rawUrl, "http://dashboard.test");
      const before = url.searchParams.get("before_id");
      const eligible = before ? rows.filter((row) => row.id < Number(before)) : rows;
      const messages = eligible.slice(-4);
      return new Response(
        JSON.stringify({
          session_id: "one",
          messages,
          pagination: {
            limit: 4,
            offset: 0,
            order: "latest",
            returned: messages.length,
            next_before_id: messages[0]?.id ?? null,
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const first = await api.getSessionMessages("one", { limit: 4, order: "latest" });
    rows.push({ id: 11, role: "user", content: "appended-between-pages" });
    const second = await api.getSessionMessages("one", {
      limit: 4,
      order: "latest",
      beforeId: first.pagination!.next_before_id!,
    });
    const third = await api.getSessionMessages("one", {
      limit: 4,
      order: "latest",
      beforeId: second.pagination!.next_before_id!,
    });

    expect(fetchMock.mock.calls[1][0]).toContain("before_id=7");
    expect([...third.messages, ...second.messages, ...first.messages].map((row) => row.id)).toEqual([
      1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    ]);
  });
});

describe("api OAuth helpers", () => {
  it("starts OAuth login in gated mode without requiring an injected session token", async () => {
    vi.stubGlobal("window", { __HERMES_AUTH_REQUIRED__: true });
    const fetchMock = jsonFetchMock({
      flow: "device_code",
      session_id: "oauth-session",
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.startOAuthLogin("openai-codex");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/providers/oauth/openai-codex/start",
      expect.objectContaining({
        body: "{}",
        credentials: "include",
        method: "POST",
      }),
    );
    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.has(SESSION_HEADER)).toBe(false);
  });

  it("still sends the injected session token for OAuth login in loopback mode", async () => {
    vi.stubGlobal("window", { __HERMES_SESSION_TOKEN__: "loopback-token" });
    const fetchMock = jsonFetchMock({
      flow: "device_code",
      session_id: "oauth-session",
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.startOAuthLogin("openai-codex");

    const headers = fetchMock.mock.calls[0][1]?.headers as Headers;
    expect(headers.get(SESSION_HEADER)).toBe("loopback-token");
  });

  it("runs provider auth mutations in gated mode via cookie auth", async () => {
    vi.stubGlobal("window", { __HERMES_AUTH_REQUIRED__: true });
    const fetchMock = jsonFetchMock({ ok: true });
    vi.stubGlobal("fetch", fetchMock);

    await api.disconnectOAuthProvider("anthropic");
    await api.submitOAuthCode("anthropic", "oauth-session", "code-123");
    await api.cancelOAuthSession("oauth-session");
    await api.revealEnvVar("OPENAI_API_KEY");

    for (const call of fetchMock.mock.calls) {
      const init = call[1] as RequestInit;
      expect(init.credentials).toBe("include");
      expect((init.headers as Headers).has(SESSION_HEADER)).toBe(false);
    }
  });
});
