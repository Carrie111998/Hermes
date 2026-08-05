import { describe, expect, it } from "vitest";
import {
  classifyDashboardLink,
  resolveDashboardRoute,
} from "./dashboard-navigation";

const CURRENT = "http://127.0.0.1:9119/chat";

describe("resolveDashboardRoute", () => {
  it("returns same-origin dashboard paths for React Router", () => {
    expect(
      resolveDashboardRoute("http://127.0.0.1:9119/models", CURRENT),
    ).toBe("/models");
    expect(
      resolveDashboardRoute("http://127.0.0.1:9119/chat?resume=abc#latest", CURRENT),
    ).toBe("/chat?resume=abc#latest");
  });

  it("accepts relative paths against the current dashboard origin", () => {
    expect(resolveDashboardRoute("/sessions", CURRENT)).toBe("/sessions");
  });

  it("leaves external origins to the normal browser link handler", () => {
    expect(resolveDashboardRoute("https://example.com/models", CURRENT)).toBeNull();
    expect(resolveDashboardRoute("http://127.0.0.1:9120/models", CURRENT)).toBeNull();
  });

  it("classifies only safe HTTP(S) origins as external", () => {
    expect(classifyDashboardLink("https://example.com/docs", CURRENT)).toEqual({
      kind: "external",
      url: "https://example.com/docs",
    });
    expect(
      classifyDashboardLink(
        "http://127.0.0.1:9119/models?token=secret",
        CURRENT,
      ),
    ).toEqual({ kind: "reject" });
    expect(classifyDashboardLink("javascript:alert(1)", CURRENT)).toEqual({
      kind: "reject",
    });
  });

  it("rejects non-http schemes and embedded credentials", () => {
    expect(resolveDashboardRoute("javascript:alert(1)", CURRENT)).toBeNull();
    expect(
      resolveDashboardRoute(
        "http://user:password@127.0.0.1:9119/models",
        CURRENT,
      ),
    ).toBeNull();
  });

  it.each(["token", "access_token", "code", "state", "secret", "api_key"])(
    "rejects sensitive %s query values",
    (key) => {
      expect(
        resolveDashboardRoute(
          `http://127.0.0.1:9119/models?${key}=do-not-route`,
          CURRENT,
        ),
      ).toBeNull();
    },
  );

  it("rejects API and asset URLs that are not SPA pages", () => {
    expect(resolveDashboardRoute("/api/dashboard/pages", CURRENT)).toBeNull();
    expect(resolveDashboardRoute("/assets/app.js", CURRENT)).toBeNull();
  });

  it("strips the BrowserRouter base path before navigation", () => {
    expect(
      resolveDashboardRoute(
        "https://example.com/hermes/models",
        "https://example.com/hermes/chat",
        "/hermes",
      ),
    ).toBe("/models");
  });

  it("rejects same-origin paths outside a configured app base", () => {
    expect(
      resolveDashboardRoute(
        "https://example.com/models",
        "https://example.com/hermes/chat",
        "/hermes",
      ),
    ).toBeNull();
  });

  it("checks protected prefixes after stripping the app base", () => {
    expect(
      resolveDashboardRoute(
        "https://example.com/hermes/api/status",
        "https://example.com/hermes/chat",
        "/hermes",
      ),
    ).toBeNull();
  });
});
