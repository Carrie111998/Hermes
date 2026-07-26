import { afterEach, describe, expect, it, vi } from "vitest";

import appSource from "../App.tsx?raw";
import badgeSource from "../components/GovernancePendingBadge.tsx?raw";
import pageSource from "../pages/GovernancePage.tsx?raw";
import { api, setManagementProfile } from "./api";
import {
  PENDING_APPROVAL_REFRESH_MS,
  startPendingApprovalPolling,
} from "./governance-pending";

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

describe("governance pending-approval polling", () => {
  it("loads immediately, refreshes every 10 seconds, and stops cleanly", async () => {
    const load = vi
      .fn()
      .mockResolvedValueOnce({ count: 2 })
      .mockResolvedValueOnce({ count: 0 });
    const onCount = vi.fn();
    const onError = vi.fn();
    let scheduledRefresh: (() => void) | undefined;
    const clearIntervalFn = vi.fn();
    const handle = startPendingApprovalPolling({
      load,
      onCount,
      onError,
      setIntervalFn: (callback, delay) => {
        expect(delay).toBe(PENDING_APPROVAL_REFRESH_MS);
        scheduledRefresh = callback;
        return 73;
      },
      clearIntervalFn,
    });

    await handle.initial;
    expect(onCount).toHaveBeenLastCalledWith(2);
    expect(scheduledRefresh).toBeTypeOf("function");

    scheduledRefresh?.();
    await vi.waitFor(() => expect(onCount).toHaveBeenLastCalledWith(0));

    handle.stop();
    expect(clearIntervalFn).toHaveBeenCalledWith(73);
    expect(onError).not.toHaveBeenCalled();
  });

  it("reports refresh failures without inventing a pending count", async () => {
    const onCount = vi.fn();
    const onError = vi.fn();
    const expectedError = new Error("offline");
    const handle = startPendingApprovalPolling({
      load: vi.fn().mockRejectedValue(expectedError),
      onCount,
      onError,
      setIntervalFn: vi.fn(() => 91),
      clearIntervalFn: vi.fn(),
    });

    await handle.initial;
    expect(onCount).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith(expectedError);
    handle.stop();
  });
});

describe("governance dashboard route", () => {
  it("is a first-class sidebar route with the required operator controls", () => {
    expect(appSource).toContain(
      'import GovernancePage from "@/pages/GovernancePage";',
    );
    expect(appSource).toContain('"/governance": GovernancePage');
    expect(appSource).toContain('path: "/governance"');
    expect(appSource).toContain(
      "<GovernancePendingBadge collapsed={collapsed} />",
    );
    expect(badgeSource).toContain("pending approval");
    expect(pageSource).toContain("Approval inbox");
    expect(pageSource).toContain("Allow once");
    expect(pageSource).toContain("Allow for target");
    expect(pageSource).toContain("Integrity failed");
    expect(pageSource).toContain("Standing approvals");
    expect(pageSource).toContain("Revoke");
    expect(pageSource).toContain("Connector health");
  });
});
