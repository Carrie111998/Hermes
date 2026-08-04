import { describe, expect, it } from "vitest";

import type { AuthMeResponse } from "@/lib/api";
import { isResumeOnlySession } from "@/lib/resume-session";

const identity = (overrides: Partial<AuthMeResponse> = {}): AuthMeResponse => ({
  user_id: "dev_1",
  email: "",
  display_name: "iPhone",
  org_id: "",
  provider: "linked-device",
  expires_at: 0,
  scopes: ["resume"],
  bound_session_id: "session-1",
  bound_profile: "default",
  ...overrides,
});

describe("isResumeOnlySession", () => {
  it("accepts only an exact bound resume identity", () => {
    expect(isResumeOnlySession(identity())).toBe(true);
    expect(isResumeOnlySession(identity({ scopes: [] }))).toBe(false);
    expect(isResumeOnlySession(identity({ scopes: ["resume", "admin"] }))).toBe(
      false,
    );
    expect(isResumeOnlySession(identity({ bound_session_id: "" }))).toBe(false);
  });
});
