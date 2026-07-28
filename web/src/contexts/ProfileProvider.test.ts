import { describe, expect, it } from "vitest";

import { initialProfileScope } from "@/contexts/profile-scope";

describe("initialProfileScope", () => {
  it("uses the dashboard launch profile when a deep link omits profile", () => {
    expect(initialProfileScope(null, "research")).toBe("research");
  });

  it("keeps an explicit URL profile ahead of the launch profile", () => {
    expect(initialProfileScope("coding", "research")).toBe("coding");
  });

  it("preserves an explicit empty profile scope", () => {
    expect(initialProfileScope("", "research")).toBe("");
  });

  it("uses the dashboard process scope when neither source is present", () => {
    expect(initialProfileScope(null, undefined)).toBe("");
  });
});
