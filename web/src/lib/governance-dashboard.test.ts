import { afterEach, describe, expect, it, vi } from "vitest";

import appSource from "../App.tsx?raw";
import pageSource from "../pages/GovernancePage.tsx?raw";
import { api, setManagementProfile } from "./api";

function jsonFetchMock(body: unknown = { ok: true }) {
  return vi.fn<typeof fetch>(
    async () =>
      new Response(JSON.stringify(body), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
  );
}

afterEach(() => {
  setManagementProfile("");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("governance dashboard API", () => {
  it("loads all governance panels in the selected profile", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock({ count: 0 });
    vi.stubGlobal("fetch", fetchMock);
    setManagementProfile("work");

    await Promise.all([
      api.getGovernanceApprovals(),
      api.getGovernanceRules(),
      api.getGovernanceConnectors(),
    ]);

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/governance/approvals?status=pending&profile=work",
      "/api/governance/rules?active_only=true&profile=work",
      "/api/governance/connectors?profile=work",
    ]);
  });

  it("sends approval decisions and rule revocations to their exact targets", async () => {
    vi.stubGlobal("window", {});
    const fetchMock = jsonFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    await api.decideGovernanceApproval("approval/id", "allow-always");
    await api.revokeGovernanceRule("rule/id");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/governance/approvals/approval%2Fid/decision",
    );
    expect(fetchMock.mock.calls[0][1]).toEqual(
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ decision: "allow-always" }),
      }),
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/governance/rules/rule%2Fid",
    );
    expect(fetchMock.mock.calls[1][1]).toEqual(
      expect.objectContaining({ method: "DELETE" }),
    );
  });
});

describe("governance dashboard route", () => {
  it("is a first-class sidebar route with the required operator controls", () => {
    expect(appSource).toContain(
      'import GovernancePage from "@/pages/GovernancePage";',
    );
    expect(appSource).toContain('"/governance": GovernancePage');
    expect(appSource).toContain('path: "/governance"');
    expect(pageSource).toContain("Approval inbox");
    expect(pageSource).toContain("Allow once");
    expect(pageSource).toContain("Allow for target");
    expect(pageSource).toContain("Integrity failed");
    expect(pageSource).toContain("Standing approvals");
    expect(pageSource).toContain("Revoke");
    expect(pageSource).toContain("Connector health");
  });
});
