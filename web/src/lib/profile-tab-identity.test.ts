import { describe, expect, it } from "vitest";

import {
  agentDisplayName,
  profileFaviconHref,
  profileTabTitle,
} from "./profile-tab-identity";

describe("agentDisplayName", () => {
  it("humanizes a profile slug into an agent name", () => {
    expect(agentDisplayName("gun")).toBe("Gun");
    expect(agentDisplayName("research_assistant")).toBe("Research Assistant");
  });

  it("uses Hermes for the dashboard's unscoped default profile", () => {
    expect(agentDisplayName("")).toBe("Hermes");
    expect(agentDisplayName("default")).toBe("Hermes");
  });
});

describe("profileTabTitle", () => {
  it("puts the agent first, followed by the current chat title and Hermes", () => {
    expect(profileTabTitle("gun", "Untitled")).toBe("Gun - Untitled - Hermes");
  });

  it("uses Untitled while a new chat has not supplied a session title", () => {
    expect(profileTabTitle("gus", null)).toBe("Gus - Untitled - Hermes");
  });
});

describe("profileFaviconHref", () => {
  it("generates an SVG favicon with the agent initial", () => {
    const href = profileFaviconHref("gun");

    expect(href).toContain("data:image/svg+xml,");
    expect(decodeURIComponent(href)).toContain(">G<");
  });

  it("assigns different profiles distinct deterministic colors", () => {
    expect(profileFaviconHref("gun")).not.toBe(profileFaviconHref("gus"));
    expect(profileFaviconHref("gun")).toBe(profileFaviconHref("gun"));
  });
});
