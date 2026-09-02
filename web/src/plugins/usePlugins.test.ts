import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  getCachedManifests,
  cacheManifests,
  canSeedLoadedFromCache,
  MANIFEST_CACHE_KEY,
} from "./usePlugins";
import type { PluginManifest } from "./types";

function makeStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem(key: string) {
      return store.get(key) ?? null;
    },
    setItem(key: string, value: string) {
      store.set(key, value);
    },
    removeItem(key: string) {
      store.delete(key);
    },
    clear() {
      store.clear();
    },
    get length() {
      return store.size;
    },
    key(index: number) {
      return Array.from(store.keys())[index] ?? null;
    },
  } as Storage;
}

const exampleManifest: PluginManifest = {
  name: "test",
  label: "Test",
  description: "A test plugin",
  icon: "Puzzle",
  version: "1.0.0",
  tab: { path: "/test" },
  entry: "index.js",
  has_api: false,
  source: "local",
};

describe("plugin manifest cache helpers", () => {
  let storage: Storage;

  beforeEach(() => {
    storage = makeStorage();
    vi.stubGlobal("sessionStorage", storage);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("getCachedManifests returns null when nothing is cached", () => {
    expect(getCachedManifests()).toBeNull();
  });

  it("getCachedManifests returns null for invalid JSON", () => {
    storage.setItem(MANIFEST_CACHE_KEY, "not-json");
    expect(getCachedManifests()).toBeNull();
  });

  it("getCachedManifests returns null for non-array JSON", () => {
    storage.setItem(MANIFEST_CACHE_KEY, JSON.stringify({ foo: "bar" }));
    expect(getCachedManifests()).toBeNull();
  });

  it("getCachedManifests returns null for scalar JSON", () => {
    storage.setItem(MANIFEST_CACHE_KEY, JSON.stringify(42));
    expect(getCachedManifests()).toBeNull();
  });

  it("getCachedManifests returns a valid manifest array", () => {
    const list: PluginManifest[] = [exampleManifest];
    cacheManifests(list);
    expect(getCachedManifests()).toEqual(list);
  });

  it("cacheManifests overwrites a previous cache on refresh", () => {
    const first: PluginManifest[] = [exampleManifest];
    cacheManifests(first);
    expect(getCachedManifests()).toEqual(first);

    const second: PluginManifest[] = [
      { ...exampleManifest, name: "updated", label: "Updated" },
    ];
    cacheManifests(second);
    expect(getCachedManifests()).toEqual(second);
  });

  it("cacheManifests swallows storage errors", () => {
    const badStorage = makeStorage();
    badStorage.setItem = () => {
      throw new Error("QuotaExceededError");
    };
    vi.stubGlobal("sessionStorage", badStorage);
    expect(() => cacheManifests([exampleManifest])).not.toThrow();
  });
});

describe("canSeedLoadedFromCache (loading seed gate)", () => {
  it("returns false when there is no cache (first visit keeps loading=true)", () => {
    expect(canSeedLoadedFromCache(null)).toBe(false);
  });

  it("returns true for an empty cached list", () => {
    expect(canSeedLoadedFromCache([])).toBe(true);
  });

  it("returns true when no cached manifest overrides /chat", () => {
    const list: PluginManifest[] = [
      exampleManifest,
      {
        ...exampleManifest,
        name: "other",
        tab: { path: "/other", override: "/skills" },
      },
    ];
    expect(canSeedLoadedFromCache(list)).toBe(true);
  });

  it("returns false when a cached manifest overrides /chat — loading must stay true so App.tsx's pluginsLoading gate keeps the persistent chat host unmounted", () => {
    const list: PluginManifest[] = [
      exampleManifest,
      {
        ...exampleManifest,
        name: "chat-replacer",
        tab: { path: "/chat-alt", override: "/chat" },
      },
    ];
    expect(canSeedLoadedFromCache(list)).toBe(false);
  });

  it("tolerates malformed cached entries missing a tab object", () => {
    const malformed = [
      { ...exampleManifest, tab: undefined },
    ] as unknown as PluginManifest[];
    expect(canSeedLoadedFromCache(malformed)).toBe(true);
  });
});

describe("exclusive shell (route-scoped)", () => {
  it("returns undefined when no exclusive manifest matches", async () => {
    const { getExclusiveShellManifest, isExclusiveShellRoute } = await import("./usePlugins");
    const list: PluginManifest[] = [exampleManifest];
    expect(getExclusiveShellManifest(list, "/")).toBeUndefined();
    expect(isExclusiveShellRoute(list, "/")).toBe(false);
  });

  it("matches exclusive shell only when override equals active route", async () => {
    const { getExclusiveShellManifest, isExclusiveShellRoute } = await import("./usePlugins");
    const exclusive: PluginManifest = {
      ...exampleManifest,
      name: "worker-studio",
      label: "Worker Studio",
      tab: { path: "/worker-studio", override: "/", shell: "exclusive" },
    };
    const list: PluginManifest[] = [exclusive];
    expect(getExclusiveShellManifest(list, "/")?.name).toBe("worker-studio");
    expect(isExclusiveShellRoute(list, "/")).toBe(true);
    // Not active on other routes
    expect(getExclusiveShellManifest(list, "/sessions")).toBeUndefined();
    expect(isExclusiveShellRoute(list, "/sessions")).toBe(false);
    // Back to exclusive
    expect(isExclusiveShellRoute(list, "/")).toBe(true);
  });

  it("normalizes trailing slashes", async () => {
    const { getExclusiveShellManifest } = await import("./usePlugins");
    const exclusive: PluginManifest = {
      ...exampleManifest,
      name: "ws",
      tab: { path: "/ws", override: "/", shell: "exclusive" },
    };
    expect(getExclusiveShellManifest([exclusive], "/")?.name).toBe("ws");
    expect(getExclusiveShellManifest([exclusive], "/")?.name).toBe("ws");
    // Manifest with trailing slash override still matches normalized "/"
    const exclusive2: PluginManifest = {
      ...exampleManifest,
      name: "ws2",
      tab: { path: "/ws2", override: "/sessions", shell: "exclusive" },
    };
    expect(getExclusiveShellManifest([exclusive2], "/sessions/")?.name).toBe("ws2");
    expect(getExclusiveShellManifest([exclusive2], "/sessions")?.name).toBe("ws2");
  });

  it("ignores standard shell and manifests without override", async () => {
    const { getExclusiveShellManifest } = await import("./usePlugins");
    const standard: PluginManifest = {
      ...exampleManifest,
      name: "std",
      tab: { path: "/std", override: "/", shell: "standard" },
    };
    const noShell: PluginManifest = {
      ...exampleManifest,
      name: "no-shell",
      tab: { path: "/no-shell", override: "/" },
    };
    const exclusiveButNoOverride: PluginManifest = {
      ...exampleManifest,
      name: "orphan",
      tab: { path: "/orphan", shell: "exclusive" },
    };
    expect(getExclusiveShellManifest([standard], "/")).toBeUndefined();
    expect(getExclusiveShellManifest([noShell], "/")).toBeUndefined();
    expect(getExclusiveShellManifest([exclusiveButNoOverride], "/")).toBeUndefined();
    expect(getExclusiveShellManifest([exclusiveButNoOverride], "/orphan")).toBeUndefined();
  });

  it("picks the first exclusive manifest that matches active route", async () => {
    const { getExclusiveShellManifest } = await import("./usePlugins");
    const a: PluginManifest = {
      ...exampleManifest,
      name: "a",
      tab: { path: "/a", override: "/", shell: "exclusive" },
    };
    const b: PluginManifest = {
      ...exampleManifest,
      name: "b",
      tab: { path: "/b", override: "/", shell: "exclusive" },
    };
    expect(getExclusiveShellManifest([a, b], "/")?.name).toBe("a");
  });

  it("exclusive shell is route-scoped: native routes remain non-exclusive", async () => {
    const { isExclusiveShellRoute } = await import("./usePlugins");
    const exclusive: PluginManifest = {
      ...exampleManifest,
      name: "ws",
      tab: { path: "/ws", override: "/", shell: "exclusive" },
    };
    const nativeRoutes = ["/sessions", "/cron", "/skills", "/plugins", "/mcp", "/config", "/channels", "/webhooks", "/system", "/profiles", "/docs", "/files", "/analytics", "/logs"];
    for (const route of nativeRoutes) {
      expect(isExclusiveShellRoute([exclusive], route)).toBe(false);
    }
    expect(isExclusiveShellRoute([exclusive], "/")).toBe(true);
  });
});
