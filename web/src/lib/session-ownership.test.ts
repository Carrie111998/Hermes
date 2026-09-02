import { describe, expect, it } from "vitest";
import {
  isGatewayOwnedSession,
  shouldConfirmResumeOwnership,
  sessionSourceFamily,
} from "./session-ownership";

describe("session-ownership", () => {
  it("detects gateway-owned active messaging sessions", () => {
    expect(
      isGatewayOwnedSession({ is_active: true, source: "discord" }),
    ).toBe(true);
    expect(
      isGatewayOwnedSession({ is_active: true, source: "api_server" }),
    ).toBe(true);
    expect(
      isGatewayOwnedSession({ is_active: true, source: "cli" }),
    ).toBe(false);
    expect(
      isGatewayOwnedSession({ is_active: false, source: "discord" }),
    ).toBe(false);
  });

  it("requires an explicit Resume handoff only for gateway-owned sessions", () => {
    expect(
      shouldConfirmResumeOwnership({ is_active: true, source: "feishu" }),
    ).toBe(true);
    expect(
      shouldConfirmResumeOwnership({ is_active: true, source: "desktop" }),
    ).toBe(false);
  });

  it("parses source families", () => {
    expect(sessionSourceFamily("discord:guild")).toBe("discord");
  });
});
